"""Copilot workflow actions: scenario confirm/run, PDF, place, ratings."""

from __future__ import annotations

import asyncio
import base64
from typing import Any
from uuid import UUID

import structlog

from backend.app.core.error_codes import (
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
    DownloadArtifact,
    GuardrailCheck,
    WorkflowState,
)
from backend.app.schemas.rules import TechnicalLimits
from backend.app.schemas.scenario import ScenarioParams
from backend.app.services import copilot_session as sessions
from backend.app.services.copilot_common import (
    ACTION_CONFIRM,
    ACTION_NEW_SCENARIO,
    ACTION_PDF,
    ACTION_PLACE,
    ACTION_PROCESS,
    ACTION_RATE_PREFIX,
    ACTION_RUN,
    ACTION_SKIP_RATING,
    ACTION_UPDATE_HOLDINGS,
    CopilotFsmError,
    _action_confirm,
    _action_new_scenario,
    _action_pdf,
    _action_place,
    _action_process,
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
    build_ord_blotter_write,
    build_rebalanced_write,
)
from backend.app.services.copilot_params import (
    format_scenario_params_markdown,
    scenario_params_to_summary,
)
from backend.app.services.interaction_ratings import (
    InteractionRatingError,
    save_interaction_rating,
)
from backend.app.services.scenario_run import (
    ScenarioServiceError,
    build_pdf_bytes,
    execute_scenario_run,
    parse_and_create_run,
    place_all_staged_orders,
    update_scenario_params,
)

logger = structlog.get_logger(__name__)

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
