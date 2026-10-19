"""Excel Copilot linear FSM: session welcome, actions, and free-text messages."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import structlog

from backend.app.core.error_codes import (
    WBP_IMPORT_CLIENT,
    WBP_SCENARIO_CLIENT,
    WBP_SCENARIO_PARSE,
)
from backend.app.schemas.copilot import (
    CopilotActionRequest,
    CopilotAgentResponse,
    CopilotSessionState,
    DownloadArtifact,
    GuardrailCheck,
    IntentClass,
    WorkflowState,
    workflow_status_label,
)
from backend.app.schemas.rules import TechnicalLimits
from backend.app.schemas.scenario import ScenarioParams
from backend.app.services import copilot_session as sessions
from backend.app.services.copilot_actions import (
    _handle_update_holdings,
    handle_action,
)
from backend.app.services.copilot_common import (
    ACTION_CONFIRM,
    ACTION_DROP,
    ACTION_ENRICH,
    ACTION_NEW_SCENARIO,
    ACTION_PDF,
    ACTION_PLACE,
    ACTION_PROCESS,
    ACTION_RATE_PREFIX,
    ACTION_REPROCESS,
    ACTION_REUSE_LAST_SCENARIO,
    ACTION_RUN,
    ACTION_SKIP_RATING,
    ACTION_UPDATE_HOLDINGS,
    CopilotFsmError,
    _HOLDINGS_RESTART_RE,
    _RATE_ACTION_IDS,
    _action_enrich,
    _action_process,
    _action_run,
    _actions_for_ids,
    _orders_placed_action_ids,
    _orders_placed_actions,
    _overlay_excel_order_qty,
    _placeholder_for,
    _post_opti_actions,
    _rating_pending,
    _response,
    _review_scenario_action_ids,
    _review_scenario_actions,
)
from backend.app.services.copilot_excel_writes import (
    ORD_BLOTTER_SHEET,
    SCRATCHPAD_SHEET,
    build_ord_blotter_write,
    build_rebalanced_write,
    build_scratchpad_write,
)
from backend.app.services.copilot_guardrails import (
    classify_identifier_intent,
    classify_intent,
    extract_id_references,
    id_write_guardrail,
    redirect_for_intent,
    validate_layer1,
)
from backend.app.services.copilot_id_recall import format_id_recall
from backend.app.services.copilot_ops_queries import (
    execute_ops_query,
    parse_ops_query,
)
from backend.app.services.copilot_params import (
    format_scenario_params_markdown,
    scenario_params_to_summary,
    try_deterministic_param_patch,
    try_llm_param_patch,
)
from backend.app.services.scenario_run import (
    ScenarioServiceError,
    parse_and_create_run,
)

logger = structlog.get_logger(__name__)

def build_welcome_response(state: CopilotSessionState) -> CopilotAgentResponse:
    return _response(
        state,
        content=(
            "To start, paste a list of securities into the sheet "
            "(include client alias, ticker, quantity and currency), "
            "select all cells and click **Process Security List** to validate.\n\n"
            "You can also ask about past portfolio updates "
            "(e.g. latest portfolios, PDF, trades)."
        ),
        actions=[_action_process()],
        badges=["Import securities"],
        accept_user_text=True,
        placeholder="Ask a lookup, or process your securities list…",
    )


# ---------------------------------------------------------------------------

def _fsm_continue_hint(state: CopilotSessionState) -> str:
    """Guide the advisor back to the most likely pending FSM action."""
    pending = list(state.pending_action_ids or [])
    label_map = {
        ACTION_PROCESS: "Process Security List",
        ACTION_REPROCESS: "Reprocess Corrected List",
        ACTION_DROP: "Drop Invalid and Proceed",
        ACTION_ENRICH: "Enrich Portfolio",
        ACTION_CONFIRM: "Confirm & Run Optimization",
        ACTION_RUN: "Run Optimization",
        ACTION_PLACE: "Place All Orders",
        ACTION_PDF: "Download PDF",
        ACTION_SKIP_RATING: "Skip rating (or tap stars)",
        ACTION_NEW_SCENARIO: "Run new scenario",
        ACTION_UPDATE_HOLDINGS: "Update holdings",
        ACTION_REUSE_LAST_SCENARIO: "Reuse last scenario",
    }
    # Prefer primary workflow actions over rating skips / secondary restarts
    preferred = [
        a
        for a in pending
        if a
        not in {
            ACTION_SKIP_RATING,
            ACTION_NEW_SCENARIO,
            ACTION_UPDATE_HOLDINGS,
            *_RATE_ACTION_IDS,
        }
    ]
    if preferred:
        labels = [label_map.get(a, a) for a in preferred[:2]]
        joined = " / ".join(f"**{x}**" for x in labels)
        return f"\n\n---\nContinue the workflow: {joined}."
    if state.current_state == WorkflowState.PORTFOLIO_ENRICHED:
        return (
            "\n\n---\nContinue the workflow: describe a market / macro scenario "
            "in chat."
        )
    if state.current_state == WorkflowState.INIT:
        return (
            "\n\n---\nContinue the workflow: select your securities table, then "
            "click **Process Security List**."
        )
    if _rating_pending(state):
        return (
            "\n\n---\nOptional: rate this simulation, or continue with "
            "**Place All Orders** / **Download PDF**."
        )
    return "\n\n---\nContinue with the workflow actions below when ready."


def _ops_states() -> frozenset[WorkflowState]:
    return frozenset(
        {
            WorkflowState.INIT,
            WorkflowState.PORTFOLIO_ENRICHED,
            WorkflowState.SCENARIO_PARSED,
            WorkflowState.OPTIMIZATION_COMPLETE,
            WorkflowState.ORDERS_PLACED,
        }
    )


async def _handle_ops_query(
    state: CopilotSessionState,
    text: str,
    *,
    advisor_id: UUID,
    limits: TechnicalLimits | None,
) -> CopilotAgentResponse | None:
    """If text is an ops query, answer it and guide back to the FSM."""
    if state.current_state not in _ops_states():
        return None
    match = parse_ops_query(text)
    if match is None:
        return None

    result = await execute_ops_query(
        match,
        advisor_id=advisor_id,
        session_portfolio_id=state.portfolio_id,
        session_scenario_run_id=state.scenario_run_id,
        session_ops_kind=state.last_ops_kind,
        session_ops_portfolio_id=state.last_ops_portfolio_id,
        session_ops_scenario_run_id=state.last_ops_scenario_run_id,
        session_ops_client_alias=state.last_ops_client_alias,
        session_ops_load_target=state.last_ops_load_target,
        limits=limits,
    )

    if result.ok:
        state.last_ops_kind = result.ops_kind
        state.last_ops_portfolio_id = result.ops_portfolio_id
        state.last_ops_scenario_run_id = result.ops_scenario_run_id
        state.last_ops_client_alias = result.ops_client_alias
        state.last_ops_load_target = result.ops_load_target
        sessions.save_session(state)

    excel_writes: list[ExcelWriteSpec] = []
    if result.excel_scratchpad_positions:
        excel_writes.append(
            build_scratchpad_write(
                result.excel_scratchpad_positions,
                hide_source_sheet=None,
            )
        )
    if result.excel_rebalanced is not None:
        excel_writes.append(
            build_rebalanced_write(
                result.excel_rebalanced,
                scenario_run_id=result.excel_rebalanced.scenario_run_id,
            )
        )

    download = None
    if result.download_base64 and result.download_filename:
        download = DownloadArtifact(
            kind="pdf",
            filename=result.download_filename,
            content_base64=result.download_base64,
        )

    guardrails: list[GuardrailCheck] = []
    if not result.ok:
        guardrails.append(
            GuardrailCheck(
                rule_id="copilot.ops_unavailable",
                severity="warning",
                message=result.content.split("\n")[0],
                suggested_fix="Check the client/portfolio exists and has a COMPLETED run.",
            )
        )

    content = result.content + _fsm_continue_hint(state)
    return _response(
        state,
        content=content,
        guardrails=guardrails,
        badges=[workflow_status_label(state.current_state)]
        + ([result.error_code] if result.error_code else ["OPS_QUERY"]),
        excel_writes=excel_writes,
        download=download,
        accept_user_text=_accepts_user_text(state),
        placeholder=_placeholder_for(state),
        show_star_rating=_rating_pending(state),
        scenario_params_summary=(
            scenario_params_to_summary(
                ScenarioParams.model_validate(state.scenario_params)
            )
            if state.scenario_params
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------


async def handle_message(
    *,
    session_id: str,
    advisor_id: UUID,
    text: str,
    limits: TechnicalLimits | None = None,
) -> CopilotAgentResponse:
    state = sessions.get_session(session_id=session_id, advisor_id=advisor_id)

    layer1 = validate_layer1(text)
    if not layer1.ok or not layer1.text:
        accept = _accepts_user_text(state)
        return _response(
            state,
            content=layer1.redirect_message or "Please rephrase your message.",
            guardrails=layer1.guardrails,
            accept_user_text=accept,
            placeholder=_placeholder_for(state) if accept else None,
        )

    cleaned = layer1.text

    # Holdings restart (button equivalent) before ops — "enrich portfolio"
    # must not be stolen by price-refresh lookups.
    if (
        ACTION_UPDATE_HOLDINGS in state.pending_action_ids
        and _HOLDINGS_RESTART_RE.search(cleaned)
    ):
        return await _handle_update_holdings(state)

    # Ops queries win over scenario prompts (then guide back to FSM).
    ops_resp = await _handle_ops_query(
        state, cleaned, advisor_id=advisor_id, limits=limits
    )
    if ops_resp is not None:
        return ops_resp

    # Identifiers: recall/audit allowed (read-only); set/write always blocked.
    id_intent, id_refs = classify_identifier_intent(cleaned)
    if id_intent == IntentClass.ID_WRITE_ATTEMPT:
        return _response(
            state,
            content=redirect_for_intent(IntentClass.ID_WRITE_ATTEMPT)
            + _fsm_continue_hint(state),
            guardrails=[id_write_guardrail()],
            accept_user_text=_accepts_user_text(state),
            placeholder=_placeholder_for(state),
            scenario_params_summary=(
                scenario_params_to_summary(
                    ScenarioParams.model_validate(state.scenario_params)
                )
                if state.scenario_params
                else None
            ),
        )
    if id_intent == IntentClass.ID_RECALL:
        refs = id_refs or extract_id_references(cleaned)
        content = format_id_recall(refs, advisor_id=advisor_id) + _fsm_continue_hint(
            state
        )
        return _response(
            state,
            content=content,
            guardrails=[
                GuardrailCheck(
                    rule_id="copilot.id_recall",
                    severity="info",
                    message="Identifier lookup is read-only; session IDs unchanged.",
                )
            ],
            accept_user_text=_accepts_user_text(state),
            placeholder=_placeholder_for(state),
            scenario_params_summary=(
                scenario_params_to_summary(
                    ScenarioParams.model_validate(state.scenario_params)
                )
                if state.scenario_params
                else None
            ),
        )

    # Free text: scenario / param edit windows
    if state.current_state == WorkflowState.PORTFOLIO_ENRICHED:
        return await _message_scenario_prompt(state, cleaned, advisor_id=advisor_id)

    if state.current_state == WorkflowState.SCENARIO_PARSED:
        if state.pending_action_ids == [ACTION_RUN]:
            # Auto-run already consumes confirm; leftover RUN-only is rare.
            return _response(
                state,
                content=(
                    redirect_for_intent(IntentClass.WORKFLOW_COMMAND)
                    + _fsm_continue_hint(state)
                ),
                actions=[_action_run()],
                accept_user_text=_accepts_user_text(state),
                placeholder=_placeholder_for(state),
            )
        return await _message_param_edit(state, cleaned)

    # Other ops-enabled states: non-ops free text → gentle redirect + guide
    if state.current_state in _ops_states():
        return _response(
            state,
            content=(
                "I can help with read-only lookups (PDF, latest portfolio, "
                "trades, house research, update prices) — or continue the workflow."
                + _fsm_continue_hint(state)
            ),
            accept_user_text=True,
            placeholder=_placeholder_for(state),
        )

    intent, checks = await classify_intent(cleaned, state=state.current_state)
    return _response(
        state,
        content=redirect_for_intent(intent) + _fsm_continue_hint(state),
        guardrails=checks
        or [
            GuardrailCheck(
                rule_id="copilot.state_gate",
                severity="warning",
                message=redirect_for_intent(intent),
            )
        ],
        accept_user_text=_accepts_user_text(state),
        placeholder=_placeholder_for(state),
    )


def _accepts_user_text(state: CopilotSessionState) -> bool:
    if state.current_state in {
        WorkflowState.INIT,
        WorkflowState.PORTFOLIO_ENRICHED,
        WorkflowState.OPTIMIZATION_COMPLETE,
        WorkflowState.ORDERS_PLACED,
    }:
        return True
    if state.current_state == WorkflowState.SCENARIO_PARSED:
        # Param edits while confirm pending; ops queries always allowed via composer.
        return True
    return False


async def _message_scenario_prompt(
    state: CopilotSessionState,
    text: str,
    *,
    advisor_id: UUID,
) -> CopilotAgentResponse:
    intent, checks = await classify_intent(text, state=state.current_state)
    if intent != IntentClass.SCENARIO_PROMPT:
        return _response(
            state,
            content=redirect_for_intent(intent),
            guardrails=checks,
            actions=[],
            accept_user_text=True,
            placeholder="Describe a market / macro scenario…",
        )

    if state.portfolio_id is None:
        raise CopilotFsmError(
            "No portfolio_id; enrich the portfolio first.",
            error_code=WBP_SCENARIO_PARSE,
            http_status=400,
        )

    try:
        parsed = await parse_and_create_run(
            portfolio_id=state.portfolio_id,
            user_prompt=text,
            advisor_id=advisor_id,
        )
    except ScenarioServiceError as exc:
        # Stay in PORTFOLIO_ENRICHED so advisor can retry
        return _response(
            state,
            content=f"Could not parse scenario: {exc}",
            guardrails=[
                GuardrailCheck(
                    rule_id="copilot.scenario_parse",
                    severity="blocking_error",
                    message=str(exc),
                    suggested_fix="Rephrase the scenario prompt and try again.",
                )
            ],
            actions=[],
            badges=["PORTFOLIO_ENRICHED", exc.error_code],
            accept_user_text=True,
            placeholder="Describe a market / macro scenario…",
        )

    params = parsed.scenario_params
    state.current_state = WorkflowState.SCENARIO_PARSED
    state.scenario_run_id = parsed.scenario_run_id
    state.scenario_params = params.model_dump(mode="json")
    state.params_dirty = False
    # New free-text scenario replaces any stashed reuse prompt.
    state.last_scenario_user_prompt = None
    state.pending_action_ids = _review_scenario_action_ids()
    sessions.save_session(state)

    return _response(
        state,
        content=format_scenario_params_markdown(params),
        actions=_review_scenario_actions(),
        badges=["SCENARIO_PARSED"],
        accept_user_text=True,
        placeholder="Edit parameters (e.g. lookback 3Y) or confirm…",
        scenario_params_summary=scenario_params_to_summary(params),
    )


async def _message_param_edit(
    state: CopilotSessionState,
    text: str,
) -> CopilotAgentResponse:
    lower = text.lower().strip()
    # Soft confirm via text
    if re.fullmatch(
        r"(confirm|approve|looks good|ok|okay|yes|lgtm)[!.,]?",
        lower,
    ):
        # Treat as confirm without going through action id path privileges —
        # still require confirm action to be pending
        if ACTION_CONFIRM in state.pending_action_ids:
            return await _handle_confirm_params(
                state, advisor_id=state.advisor_id
            )

    intent, checks = await classify_intent(text, state=state.current_state)
    if intent in {
        IntentClass.PROMPT_INJECTION,
        IntentClass.OFF_TOPIC_NONSENSE,
        IntentClass.ID_WRITE_ATTEMPT,
    }:
        return _response(
            state,
            content=redirect_for_intent(intent),
            guardrails=checks
            or (
                [id_write_guardrail()]
                if intent == IntentClass.ID_WRITE_ATTEMPT
                else []
            ),
            actions=_review_scenario_actions(),
            accept_user_text=True,
            placeholder="Edit parameters or confirm…",
            scenario_params_summary=(
                scenario_params_to_summary(
                    ScenarioParams.model_validate(state.scenario_params)
                )
                if state.scenario_params
                else None
            ),
        )

    if state.scenario_params is None:
        raise CopilotFsmError(
            "No scenario params in session.",
            error_code=WBP_SCENARIO_PARAMS,
            http_status=400,
        )

    current = ScenarioParams.model_validate(state.scenario_params)
    patched = try_deterministic_param_patch(text, current)
    if patched is None:
        patched = await try_llm_param_patch(text, current)

    if patched is None:
        return _response(
            state,
            content=(
                "I couldn't apply that as a parameter edit. "
                "Try phrases like “lookback 3Y”, “tone risk_off”, "
                "“risk aversion 2.5”, or “freeze AAPL, MSFT”, "
                "or click **Confirm & Run Optimization**. "
                "To change securities, click **Update holdings**."
            ),
            actions=_review_scenario_actions(),
            accept_user_text=True,
            placeholder="Edit parameters or confirm…",
            scenario_params_summary=scenario_params_to_summary(current),
        )

    state.scenario_params = patched.model_dump(mode="json")
    state.params_dirty = True
    state.pending_action_ids = _review_scenario_action_ids()
    sessions.save_session(state)

    return _response(
        state,
        content=(
            "Updated draft parameters (not yet saved):\n\n"
            + format_scenario_params_markdown(patched)
        ),
        actions=_review_scenario_actions(),
        badges=["SCENARIO_PARSED", "PARAMS_DIRTY"],
        accept_user_text=True,
        placeholder="Further edits, or confirm…",
        scenario_params_summary=scenario_params_to_summary(patched),
    )


__all__ = [
    "ACTION_CONFIRM",
    "ACTION_ENRICH",
    "ACTION_NEW_SCENARIO",
    "ACTION_PDF",
    "ACTION_PLACE",
    "ACTION_PROCESS",
    "ACTION_RUN",
    "ACTION_SKIP_RATING",
    "ACTION_UPDATE_HOLDINGS",
    "ACTION_REUSE_LAST_SCENARIO",
    "CopilotFsmError",
    "ORD_BLOTTER_SHEET",
    "SCRATCHPAD_SHEET",
    "_orders_placed_action_ids",
    "_overlay_excel_order_qty",
    "_response",
    "build_ord_blotter_write",
    "build_rebalanced_write",
    "build_scratchpad_write",
    "build_welcome_response",
    "handle_action",
    "handle_message",
    "try_deterministic_param_patch",
]
