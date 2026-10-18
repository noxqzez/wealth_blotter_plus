"""Copilot FSM action handlers (import → optimize → place)."""

from __future__ import annotations

import asyncio
import base64
import re
from typing import Any
from uuid import UUID

import structlog

from backend.app.core.error_codes import (
    WBP_IMPORT_BUILD,
    WBP_IMPORT_CLIENT,
    WBP_IMPORT_VALIDATE,
    WBP_SCENARIO_CLIENT,
    WBP_SCENARIO_PARAMS,
    WBP_SCENARIO_PARSE,
    WBP_SCENARIO_PDF,
    WBP_SCENARIO_RUN,
)
from backend.app.schemas.copilot import (
    CopilotActionRequest,
    CopilotAgentResponse,
    CopilotSessionState,
    GuardrailCheck,
    WorkflowState,
)
from backend.app.schemas.portfolio_import import (
    BuildBaseRequest,
    BuildBaseResponse,
    ImportValidationRequest,
    RawImportRow,
)
from backend.app.schemas.rules import TechnicalLimits
from backend.app.schemas.scenario import ScenarioParams
from backend.app.services import copilot_session as sessions
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
    ACTION_RUN,
    ACTION_SKIP_RATING,
    ACTION_UPDATE_HOLDINGS,
    CopilotFsmError,
    _TICKER_IN_ERROR_RE,
    _action_confirm,
    _action_drop,
    _action_enrich,
    _action_new_scenario,
    _action_pdf,
    _action_place,
    _action_process,
    _action_reprocess,
    _action_run,
    _action_skip_rating,
    _action_update_holdings,
    _actions_for_ids,
    _orders_placed_action_ids,
    _orders_placed_actions,
    _overlay_excel_order_qty,
    _placeholder_for,
    _post_opti_action_ids,
    _post_opti_actions,
    _rating_pending,
    _rating_prompt_markdown,
    _response,
    _review_scenario_action_ids,
    _review_scenario_actions,
    _sync_rating_state,
)
from backend.app.services.copilot_excel_writes import (
    ORD_BLOTTER_SHEET,
    REBALANCED_SHEET,
    SCRATCHPAD_SHEET,
    build_ord_blotter_write,
    build_rebalanced_write,
    build_scratchpad_write,
)
from backend.app.services.copilot_params import (
    format_scenario_params_markdown,
    scenario_params_to_summary,
)
from backend.app.services.interaction_ratings import (
    InteractionRatingError,
    save_interaction_rating,
)
from backend.app.services.portfolio_import import (
    build_base,
    set_portfolio_import_hitl,
    validate_import,
)
from backend.app.services.portfolio_normalize import ImportValidationError
from backend.app.services.scenario_run import (
    ScenarioServiceError,
    build_pdf_bytes,
    execute_scenario_run,
    parse_and_create_run,
    place_all_staged_orders,
    update_scenario_params,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------


def _rows_from_payload(rows: list[dict[str, Any]] | None) -> list[RawImportRow]:
    if not rows:
        raise CopilotFsmError(
            "Excel selection rows are required for this action.",
            error_code=WBP_IMPORT_CLIENT,
            http_status=400,
        )
    try:
        return [RawImportRow.model_validate(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        raise CopilotFsmError(
            f"Invalid import rows: {exc}",
            error_code=WBP_IMPORT_CLIENT,
            http_status=400,
        ) from exc


def _extract_invalid_tickers(error_message: str) -> list[str]:
    found = _TICKER_IN_ERROR_RE.findall(error_message or "")
    # Also catch "for TICKER" style
    extra = re.findall(
        r"(?:for ticker|ticker)\s+([A-Za-z0-9.\-]+)",
        error_message or "",
        flags=re.IGNORECASE,
    )
    tickers: list[str] = []
    for t in [*found, *extra]:
        u = t.strip().upper()
        if u and u not in tickers:
            tickers.append(u)
    return tickers


async def _validate_rows(
    rows: list[RawImportRow],
    *,
    state: CopilotSessionState,
    source_sheet_name: str | None,
    limits: TechnicalLimits | None = None,
) -> CopilotAgentResponse:
    payload = ImportValidationRequest(rows=rows)
    try:
        result = await validate_import(payload, advisor_id=state.advisor_id)
    except ImportValidationError as exc:
        err = str(exc)
        state.current_state = WorkflowState.BLOTTER_HAS_ERRORS
        state.last_validation_error = err
        state.last_import_rows = [r.model_dump(mode="json") for r in rows]
        if source_sheet_name:
            state.last_source_sheet = source_sheet_name
        state.pending_action_ids = [ACTION_REPROCESS, ACTION_DROP]
        sessions.save_session(state)
        return _response(
            state,
            content=(
                "### Blotter validation errors\n\n"
                f"{err}\n\n"
                "Fix the list in Excel and **Reprocess**, or try "
                "**Drop Invalid and Proceed** if the error names specific tickers."
            ),
            actions=[_action_reprocess(), _action_drop()],
            guardrails=[
                GuardrailCheck(
                    rule_id="copilot.blotter_validation",
                    severity="blocking_error",
                    message=err,
                    suggested_fix="Correct tickers / quantities in the sheet.",
                )
            ],
            badges=["BLOTTER_HAS_ERRORS", WBP_IMPORT_VALIDATE],
            accept_user_text=False,
        )

    state.current_state = WorkflowState.BLOTTER_VALIDATED
    state.portfolio_id = result.portfolio_id
    state.draft_id = result.draft_id
    state.last_validation_error = None
    state.last_import_rows = [r.model_dump(mode="json") for r in rows]
    if source_sheet_name:
        state.last_source_sheet = source_sheet_name
    state.pending_action_ids = [ACTION_ENRICH]
    sessions.save_session(state)

    try:
        set_portfolio_import_hitl(result.portfolio_id, hitl=bool(state.import_hitl))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "portfolio_import_hitl_persist_failed",
            portfolio_id=str(result.portfolio_id),
            import_hitl=bool(state.import_hitl),
            error=str(exc),
        )

    # Clean first-pass validation: enrich immediately (no extra click).
    if not state.import_hitl:
        enrich_resp = await _handle_enrich(state, limits=limits)
        notes = "\n".join(f"- {m}" for m in result.messages) if result.messages else ""
        prefix_parts = [
            f"Blotter validated for **{result.client_alias}** "
            f"({len(result.positions)} position(s)). Auto-enriching…",
        ]
        if notes:
            prefix_parts.append(notes)
        prefix = "\n".join(prefix_parts) + "\n\n"
        return enrich_resp.model_copy(update={"content": prefix + enrich_resp.content})

    notes = "\n".join(f"- {m}" for m in result.messages) if result.messages else ""
    parts = [
        f"Blotter validated for **{result.client_alias}** "
        f"({len(result.positions)} position(s)).",
    ]
    if notes:
        parts.append(notes)
    parts.append(
        f"Next: **Enrich Portfolio** to pull market data into `{SCRATCHPAD_SHEET}`."
    )
    parts.append("Import HITL flagged (reprocess / drop invalid).")
    return _response(
        state,
        content="\n\n".join(parts),
        actions=[_action_enrich()],
        badges=["BLOTTER_VALIDATED", "IMPORT_HITL"],
        accept_user_text=False,
    )


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


async def handle_action(
    *,
    session_id: str,
    advisor_id: UUID,
    request: CopilotActionRequest,
    limits: TechnicalLimits | None = None,
) -> CopilotAgentResponse:
    state = sessions.get_session(session_id=session_id, advisor_id=advisor_id)
    action_id = request.action_id.strip()

    if action_id not in state.pending_action_ids:
        raise CopilotFsmError(
            f"Action '{action_id}' is not available in state {state.current_state.value}.",
            error_code=WBP_SCENARIO_CLIENT,
            http_status=400,
        )

    if action_id in {ACTION_PROCESS, ACTION_REPROCESS}:
        # Fresh Process = clean first pass; Reprocess = advisor fixed the list (HITL).
        if action_id == ACTION_PROCESS:
            state.import_hitl = False
        else:
            state.import_hitl = True
        sessions.save_session(state)
        rows = _rows_from_payload(request.rows)
        return await _validate_rows(
            rows,
            state=state,
            source_sheet_name=request.source_sheet_name,
            limits=limits,
        )

    if action_id == ACTION_DROP:
        state.import_hitl = True
        sessions.save_session(state)
        return await _handle_drop_invalid(state, limits=limits)

    if action_id == ACTION_ENRICH:
        return await _handle_enrich(state, limits=limits)

    if action_id == ACTION_CONFIRM:
        return await _handle_confirm_params(
            state, advisor_id=advisor_id, limits=limits
        )

    if action_id == ACTION_RUN:
        return await _handle_run(state, advisor_id=advisor_id, limits=limits)

    if action_id == ACTION_PDF:
        return await _handle_pdf(state, advisor_id=advisor_id)

    if action_id == ACTION_PLACE:
        return await _handle_place_orders(state, advisor_id=advisor_id, request=request)

    if action_id == ACTION_NEW_SCENARIO:
        return await _handle_run_new_scenario(state)

    if action_id == ACTION_UPDATE_HOLDINGS:
        return await _handle_update_holdings(state)

    if action_id == ACTION_SKIP_RATING or action_id.startswith(ACTION_RATE_PREFIX):
        return await _handle_rating_action(
            state, advisor_id=advisor_id, action_id=action_id
        )

    raise CopilotFsmError(
        f"Unknown action '{action_id}'.",
        error_code=WBP_SCENARIO_CLIENT,
        http_status=400,
    )


async def _clear_scenario_draft(state: CopilotSessionState) -> None:
    """Drop active scenario draft so the next prompt creates a new scenario_run."""
    state.scenario_run_id = None
    state.scenario_params = None
    state.params_dirty = False
    state.rating_captured = False
    state.rating_for_run_id = None


async def _handle_run_new_scenario(
    state: CopilotSessionState,
) -> CopilotAgentResponse:
    """Optimization complete / Orders placed → Enter scenario (same portfolio)."""
    if state.current_state not in {
        WorkflowState.OPTIMIZATION_COMPLETE,
        WorkflowState.ORDERS_PLACED,
    }:
        raise CopilotFsmError(
            "Run new scenario is only available after optimization or orders placed.",
            error_code=WBP_SCENARIO_CLIENT,
            http_status=400,
        )
    if state.portfolio_id is None:
        raise CopilotFsmError(
            "No portfolio on session; import and enrich holdings first.",
            error_code=WBP_IMPORT_CLIENT,
            http_status=400,
        )

    await _clear_scenario_draft(state)
    state.current_state = WorkflowState.PORTFOLIO_ENRICHED
    state.pending_action_ids = []
    sessions.save_session(state)

    return _response(
        state,
        content=(
            "Starting a **new scenario** on the same portfolio "
            f"`{state.portfolio_id}`.\n\n"
            "Prior completed runs and blotter orders are kept for history. "
            "Describe a new market / macro scenario in chat "
            "(a new scenarioID will be created)."
        ),
        actions=[],
        badges=["Enter scenario"],
        accept_user_text=True,
        placeholder="Describe a market / macro scenario…",
    )


async def _handle_update_holdings(
    state: CopilotSessionState,
) -> CopilotAgentResponse:
    """Import securities again (add/change weights) → enrich → Enter scenario."""
    if state.current_state not in {
        WorkflowState.SCENARIO_PARSED,
        WorkflowState.OPTIMIZATION_COMPLETE,
        WorkflowState.ORDERS_PLACED,
    }:
        raise CopilotFsmError(
            "Update holdings is available after scenario review, optimization, "
            "or orders placed.",
            error_code=WBP_SCENARIO_CLIENT,
            http_status=400,
        )

    await _clear_scenario_draft(state)
    state.current_state = WorkflowState.INIT
    state.pending_action_ids = [ACTION_PROCESS]
    # Keep portfolio_id so same client_alias can reattach; draft cleared on process.
    state.draft_id = None
    state.last_import_rows = None
    state.last_validation_error = None
    state.import_hitl = False
    sessions.save_session(state)

    return _response(
        state,
        content=(
            "**Update holdings:** select your securities table in Excel "
            "(add names or change weights; same `client_alias` keeps this "
            "portfolio; new alias starts fresh), then click "
            "**Process Security List**.\n\n"
            "Prior scenario runs and orders stay in history. After enrich, "
            "you’ll return to **Enter scenario** against the updated book."
        ),
        actions=[_action_process()],
        badges=["Import securities"],
        accept_user_text=True,
        placeholder="Ask a lookup, or process your securities list…",
    )


async def _handle_drop_invalid(
    state: CopilotSessionState,
    *,
    limits: TechnicalLimits | None = None,
) -> CopilotAgentResponse:
    err = state.last_validation_error or ""
    tickers = _extract_invalid_tickers(err)
    raw_rows = state.last_import_rows or []
    if not tickers or not raw_rows:
        state.pending_action_ids = [ACTION_REPROCESS, ACTION_DROP]
        sessions.save_session(state)
        return _response(
            state,
            content=(
                "I couldn't identify specific invalid tickers from the last error "
                f"({err or 'unknown'}).\n\n"
                "Please fix the sheet in Excel and click **Reprocess Corrected List**."
            ),
            actions=[_action_reprocess(), _action_drop()],
            badges=["BLOTTER_HAS_ERRORS"],
            accept_user_text=False,
        )

    drop_set = {t.upper() for t in tickers}
    # Also drop crypto bases without -USD suffix match
    filtered: list[dict[str, Any]] = []
    for row in raw_rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        base = ticker.removesuffix("-USD")
        if ticker in drop_set or base in drop_set:
            continue
        filtered.append(row)

    if len(filtered) == len(raw_rows):
        state.pending_action_ids = [ACTION_REPROCESS, ACTION_DROP]
        sessions.save_session(state)
        return _response(
            state,
            content=(
                f"No rows matched invalid tickers ({', '.join(tickers)}). "
                "Fix the sheet and **Reprocess**."
            ),
            actions=[_action_reprocess(), _action_drop()],
            badges=["BLOTTER_HAS_ERRORS"],
            accept_user_text=False,
        )

    if not filtered:
        state.pending_action_ids = [ACTION_REPROCESS, ACTION_DROP]
        sessions.save_session(state)
        return _response(
            state,
            content=(
                "Dropping those tickers would leave an empty list. "
                "Please correct the sheet and **Reprocess**."
            ),
            actions=[_action_reprocess(), _action_drop()],
            badges=["BLOTTER_HAS_ERRORS"],
            accept_user_text=False,
        )

    logger.info(
        "copilot_drop_invalid",
        session_id=state.session_id,
        dropped=list(drop_set),
        remaining=len(filtered),
    )
    rows = [RawImportRow.model_validate(r) for r in filtered]
    return await _validate_rows(
        rows,
        state=state,
        source_sheet_name=state.last_source_sheet,
        limits=limits,
    )


async def _handle_enrich(
    state: CopilotSessionState,
    *,
    limits: TechnicalLimits | None,
) -> CopilotAgentResponse:
    if state.portfolio_id is None:
        raise CopilotFsmError(
            "No portfolio_id on session; validate the blotter first.",
            error_code=WBP_IMPORT_CLIENT,
            http_status=400,
        )
    try:
        result: BuildBaseResponse = await build_base(
            BuildBaseRequest(
                portfolio_id=state.portfolio_id,
                draft_id=state.draft_id,
            ),
            limits=limits,
        )
    except ImportValidationError as exc:
        raise CopilotFsmError(
            str(exc),
            error_code=WBP_IMPORT_BUILD,
            http_status=400,
        ) from exc

    write = build_scratchpad_write(
        result.positions,
        hide_source_sheet=state.last_source_sheet,
    )
    state.current_state = WorkflowState.PORTFOLIO_ENRICHED
    state.portfolio_id = result.portfolio_id
    state.draft_id = result.draft_id
    state.pending_action_ids = []
    sessions.save_session(state)

    warnings = "\n".join(f"- {w}" for w in result.warnings) if result.warnings else ""
    parts = [
        f"Portfolio enriched for **{result.client_alias}** "
        f"({len(result.positions)} position(s)) for portfolioID: `{result.portfolio_id}`. "
        f"Values in `{SCRATCHPAD_SHEET}` tab.",
    ]
    if warnings:
        parts.append(warnings)
    parts.append(
        "Describe a market scenario in the chat "
        "(e.g. “Fed cuts rates 50bp; risk-on equities, 3M horizon”)."
    )
    return _response(
        state,
        content="\n\n".join(parts),
        actions=[],
        badges=["Enter scenario"],
        excel_writes=[write],
        accept_user_text=True,
        placeholder="Describe a market / macro scenario…",
    )


async def _handle_confirm_params(
    state: CopilotSessionState,
    *,
    advisor_id: UUID,
    limits: TechnicalLimits | None = None,
) -> CopilotAgentResponse:
    if state.scenario_run_id is None or state.scenario_params is None:
        raise CopilotFsmError(
            "No scenario params to confirm.",
            error_code=WBP_SCENARIO_PARAMS,
            http_status=400,
        )
    params = ScenarioParams.model_validate(state.scenario_params)
    if state.params_dirty:
        try:
            updated = await update_scenario_params(
                scenario_run_id=state.scenario_run_id,
                human_params=params,
                advisor_id=advisor_id,
            )
            params = updated.scenario_params
            state.scenario_params = params.model_dump(mode="json")
        except ScenarioServiceError as exc:
            raise CopilotFsmError(
                str(exc),
                error_code=exc.error_code,
                http_status=exc.http_status,
                support=exc.http_status >= 500,
            ) from exc

    state.params_dirty = False
    # Confirm immediately kicks off optimization (no separate Run click).
    state.pending_action_ids = [ACTION_RUN]
    sessions.save_session(state)

    run_resp = await _handle_run(state, advisor_id=advisor_id, limits=limits)
    run_id = state.scenario_run_id
    confirmed = (
        f"Scenario parameters confirmed for scenario `{run_id}`. "
        "Starting optimization…\n\n"
    )
    return run_resp.model_copy(update={"content": confirmed + run_resp.content})


async def _handle_rating_action(
    state: CopilotSessionState,
    *,
    advisor_id: UUID,
    action_id: str,
) -> CopilotAgentResponse:
    """Optional 1–5 star feedback (or skip) after opti_folio; one per simulation."""
    if state.current_state != WorkflowState.OPTIMIZATION_COMPLETE:
        raise CopilotFsmError(
            "Rating is only available after optimization completes.",
            error_code=WBP_SCENARIO_CLIENT,
            http_status=400,
        )
    if state.scenario_run_id is None:
        raise CopilotFsmError(
            "No scenario_run_id for rating.",
            error_code=WBP_SCENARIO_CLIENT,
            http_status=400,
        )

    _sync_rating_state(state)
    if state.rating_captured and state.rating_for_run_id == state.scenario_run_id:
        state.pending_action_ids = _post_opti_action_ids(state)
        sessions.save_session(state)
        return _response(
            state,
            content=(
                "This simulation was already rated (one rating per run). "
                "Continue with **Place All Orders**, **Download PDF**, "
                "**Run new scenario**, or **Update holdings**."
            ),
            actions=_post_opti_actions(state),
            badges=["OPTIMIZATION_COMPLETE", "RATING_ALREADY_CAPTURED"],
            accept_user_text=True,
        )

    if action_id == ACTION_SKIP_RATING:
        state.rating_captured = True
        state.rating_for_run_id = state.scenario_run_id
        state.pending_action_ids = _post_opti_action_ids(state)
        sessions.save_session(state)
        return _response(
            state,
            content=(
                "Rating skipped. Continue with **Place All Orders**, "
                "**Download PDF**, **Run new scenario**, or **Update holdings**."
            ),
            actions=_post_opti_actions(state),
            badges=["OPTIMIZATION_COMPLETE", "RATING_SKIPPED"],
            accept_user_text=True,
        )

    if not action_id.startswith(ACTION_RATE_PREFIX):
        raise CopilotFsmError(
            f"Unknown rating action '{action_id}'.",
            error_code=WBP_SCENARIO_CLIENT,
            http_status=400,
        )
    try:
        stars = int(action_id.removeprefix(ACTION_RATE_PREFIX))
    except ValueError as exc:
        raise CopilotFsmError(
            f"Invalid rating action '{action_id}'.",
            error_code=WBP_SCENARIO_CLIENT,
            http_status=400,
        ) from exc
    if stars < 1 or stars > 5:
        raise CopilotFsmError(
            "Rating must be between 1 and 5 stars.",
            error_code=WBP_SCENARIO_CLIENT,
            http_status=400,
        )

    try:
        await asyncio.to_thread(
            save_interaction_rating,
            advisor_id=advisor_id,
            scenario_run_id=state.scenario_run_id,
            rating=stars,
            portfolio_id=state.portfolio_id,
            session_id=state.session_id,
        )
    except InteractionRatingError as exc:
        if exc.already_rated:
            state.rating_captured = True
            state.rating_for_run_id = state.scenario_run_id
            state.pending_action_ids = _post_opti_action_ids(state)
            sessions.save_session(state)
            return _response(
                state,
                content=(
                    "This simulation was already rated (one rating per run). "
                    "Continue with **Place All Orders**, **Download PDF**, "
                    "**Run new scenario**, or **Update holdings**."
                ),
                actions=_post_opti_actions(state),
                badges=["OPTIMIZATION_COMPLETE", "RATING_ALREADY_CAPTURED"],
                accept_user_text=True,
            )
        raise CopilotFsmError(
            str(exc),
            error_code=WBP_SCENARIO_SERVER,
            http_status=500,
            support=True,
        ) from exc

    state.rating_captured = True
    state.rating_for_run_id = state.scenario_run_id
    state.pending_action_ids = _post_opti_action_ids(state)
    sessions.save_session(state)
    stars_label = "★" * stars + "☆" * (5 - stars)
    return _response(
        state,
        content=(
            f"Thanks — recorded **{stars}/5** ({stars_label}) for this simulation. "
            "Continue with **Place All Orders**, **Download PDF**, "
            "**Run new scenario**, or **Update holdings**."
        ),
        actions=_post_opti_actions(state),
        badges=["OPTIMIZATION_COMPLETE", "RATING_SAVED"],
        accept_user_text=True,
    )


async def _handle_run(
    state: CopilotSessionState,
    *,
    advisor_id: UUID,
    limits: TechnicalLimits | None,
) -> CopilotAgentResponse:
    if state.scenario_run_id is None:
        raise CopilotFsmError(
            "No scenario_run_id; parse a scenario first.",
            error_code=WBP_SCENARIO_RUN,
            http_status=400,
        )
    # Persist dirty params before run if advisor skipped explicit confirm
    if state.params_dirty and state.scenario_params is not None:
        try:
            await update_scenario_params(
                scenario_run_id=state.scenario_run_id,
                human_params=ScenarioParams.model_validate(state.scenario_params),
                advisor_id=advisor_id,
            )
            state.params_dirty = False
        except ScenarioServiceError as exc:
            raise CopilotFsmError(
                str(exc),
                error_code=exc.error_code,
                http_status=exc.http_status,
                support=exc.http_status >= 500,
            ) from exc

    try:
        result = await execute_scenario_run(
            scenario_run_id=state.scenario_run_id,
            advisor_id=advisor_id,
            limits=limits,
        )
    except ScenarioServiceError as exc:
        raise CopilotFsmError(
            str(exc),
            error_code=exc.error_code,
            http_status=exc.http_status,
            support=exc.http_status >= 500,
        ) from exc

    if result.status != "COMPLETED":
        # Allow param edits + confirm, holdings update, then retry run
        state.pending_action_ids = [
            ACTION_CONFIRM,
            ACTION_RUN,
            ACTION_UPDATE_HOLDINGS,
        ]
        sessions.save_session(state)
        msg = result.error_message or "Optimization failed."
        return _response(
            state,
            content=f"Optimization did not complete: {msg}",
            actions=[
                _action_confirm(),
                _action_run(),
                _action_update_holdings(),
            ],
            guardrails=[
                GuardrailCheck(
                    rule_id="copilot.optimization_failed",
                    severity="blocking_error",
                    message=msg,
                    suggested_fix=(
                        "Adjust parameters, **Update holdings**, or try again."
                    ),
                )
            ],
            badges=["SCENARIO_PARSED", result.error_code or "FAILED"],
            accept_user_text=True,
            placeholder="Edit scenario parameters…",
            scenario_params_summary=(
                scenario_params_to_summary(result.scenario_params)
                if result.scenario_params
                else None
            ),
        )

    writes = [
        build_rebalanced_write(result, scenario_run_id=result.scenario_run_id),
        build_ord_blotter_write(result, scenario_run_id=result.scenario_run_id),
    ]
    state.current_state = WorkflowState.OPTIMIZATION_COMPLETE
    state.scenario_run_id = result.scenario_run_id
    state.portfolio_id = result.portfolio_id
    state.scenario_params = result.scenario_params.model_dump(mode="json")
    # New simulation → re-offer optional rating (unless DB already has one).
    state.rating_for_run_id = result.scenario_run_id
    state.rating_captured = False
    _sync_rating_state(state)
    state.pending_action_ids = _post_opti_action_ids(state)
    sessions.save_session(state)

    n_trades = len(result.staged_trades or [])
    rating_block = _rating_prompt_markdown() if _rating_pending(state) else ""
    past_analogue_block = ""
    if result.past_analogue is not None and result.past_analogue.enabled:
        summary = (result.past_analogue.chat_summary or "").strip()
        if summary:
            past_analogue_block = f"\n\n{summary}\n"
    return _response(
        state,
        content=(
            f"Optimization complete. Wrote `{REBALANCED_SHEET}` and "
            f"`{ORD_BLOTTER_SHEET}` ({n_trades} staged order(s), status **Pending**).\n\n"
            "Review (and optionally edit) orders on `ord_blotter`, then "
            "**Place All Orders** to finish. **Download PDF**, "
            "**Run new scenario**, or **Update holdings** anytime."
            f"{past_analogue_block}"
            f"{rating_block}"
        ),
        actions=_post_opti_actions(state),
        badges=["OPTIMIZATION_COMPLETE"],
        excel_writes=writes,
        accept_user_text=True,
        placeholder=_placeholder_for(state),
        scenario_params_summary=scenario_params_to_summary(result.scenario_params),
    )


async def _handle_pdf(
    state: CopilotSessionState,
    *,
    advisor_id: UUID,
) -> CopilotAgentResponse:
    if state.scenario_run_id is None:
        raise CopilotFsmError(
            "No scenario_run_id for PDF.",
            error_code=WBP_SCENARIO_PDF,
            http_status=400,
        )
    try:
        pdf_bytes, filename, _alias = await build_pdf_bytes(
            scenario_run_id=state.scenario_run_id,
            advisor_id=advisor_id,
        )
    except ScenarioServiceError as exc:
        raise CopilotFsmError(
            str(exc),
            error_code=exc.error_code,
            http_status=exc.http_status,
            support=exc.http_status >= 500,
        ) from exc

    # Keep Place All available until orders are placed; preserve rating prompt.
    if state.current_state == WorkflowState.ORDERS_PLACED:
        state.pending_action_ids = _orders_placed_action_ids()
        actions = _orders_placed_actions()
        badges = ["ORDERS_PLACED", "PDF_READY"]
    else:
        _sync_rating_state(state)
        state.pending_action_ids = _post_opti_action_ids(state)
        actions = _post_opti_actions(state)
        badges = ["OPTIMIZATION_COMPLETE", "PDF_READY"]
    sessions.save_session(state)
    artifact = DownloadArtifact(
        kind="pdf",
        filename=filename,
        content_base64=base64.b64encode(pdf_bytes).decode("ascii"),
    )
    return _response(
        state,
        content=f"PDF ready: **{filename}**.",
        actions=actions,
        badges=badges,
        download=artifact,
        accept_user_text=True,
        placeholder=_placeholder_for(state),
    )


async def _handle_place_orders(
    state: CopilotSessionState,
    *,
    advisor_id: UUID,
    request: CopilotActionRequest,
) -> CopilotAgentResponse:
    if state.scenario_run_id is None:
        raise CopilotFsmError(
            "No scenario_run_id; run optimization first.",
            error_code=WBP_SCENARIO_CLIENT,
            http_status=400,
        )
    try:
        placed = await asyncio.to_thread(
            place_all_staged_orders,
            scenario_run_id=state.scenario_run_id,
            advisor_id=advisor_id,
            excel_orders=request.order_rows,
        )
    except ScenarioServiceError as exc:
        raise CopilotFsmError(
            str(exc),
            error_code=exc.error_code,
            http_status=exc.http_status,
            support=exc.http_status >= 500,
        ) from exc

    orders = list(placed.get("orders") or [])
    orders_hitl = bool(placed.get("orders_hitl"))
    notes = list(placed.get("mismatch_notes") or [])
    # Keep advisor blotter qty on the sheet (HITL edits); DB still has staged qty.
    display_orders = _overlay_excel_order_qty(orders, request.order_rows)
    write = build_ord_blotter_write(
        display_orders, scenario_run_id=state.scenario_run_id
    )

    state.current_state = WorkflowState.ORDERS_PLACED
    state.pending_action_ids = _orders_placed_action_ids()
    sessions.save_session(state)

    hitl_line = (
        "\n\n_Orders HITL flagged — blotter differed from system-staged orders._"
        if orders_hitl
        else ""
    )
    detail = ""
    if notes:
        detail = "\n" + "\n".join(f"- {n}" for n in notes[:5])
    return _response(
        state,
        content=(
            f"Placed **{len(orders)}** order(s) (dummy OMS → **Executed**). "
            f"`{ORD_BLOTTER_SHEET}` updated.{hitl_line}{detail}\n\n"
            "Workflow complete for this scenario. To start again, click "
            "**Process Security List** with a new (or updated) holdings table."
        ),
        actions=_orders_placed_actions(),
        badges=["ORDERS_PLACED"] + (["ORDERS_HITL"] if orders_hitl else []),
        excel_writes=[write],
        accept_user_text=True,
        placeholder=_placeholder_for(state),
    )



