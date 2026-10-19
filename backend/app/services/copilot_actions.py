"""Copilot FSM action handlers (import → optimize → place)."""

from __future__ import annotations

from uuid import UUID

from backend.app.core.error_codes import WBP_SCENARIO_CLIENT
from backend.app.schemas.copilot import CopilotActionRequest, CopilotAgentResponse
from backend.app.schemas.rules import TechnicalLimits
from backend.app.services import copilot_session as sessions
from backend.app.services.copilot_actions_import import (
    _handle_drop_invalid,
    _handle_enrich,
    _rows_from_payload,
    _validate_rows,
)
from backend.app.schemas.copilot import WorkflowState
from backend.app.services.copilot_actions_workflow import (
    _handle_confirm_params,
    _handle_pdf,
    _handle_place_orders,
    _handle_rating_action,
    _handle_reuse_last_scenario,
    _handle_run,
    _handle_run_new_scenario,
    _handle_update_holdings,
    prepare_holdings_restart,
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
)

# Re-exports used by tests / monkeypatches against this module.
from backend.app.services.portfolio_import import (  # noqa: F401
    build_base,
    set_portfolio_import_hitl,
    validate_import,
)
from backend.app.services.interaction_ratings import (  # noqa: F401
    save_interaction_rating,
)
from backend.app.services.scenario_run import (  # noqa: F401
    execute_scenario_run,
    parse_and_create_run,
    place_all_staged_orders,
)


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
        if action_id == ACTION_PROCESS:
            # ORDERS_PLACED Process is a holdings restart alias — clear draft first.
            if state.current_state == WorkflowState.ORDERS_PLACED:
                prepare_holdings_restart(state)
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

    if action_id == ACTION_REUSE_LAST_SCENARIO:
        return await _handle_reuse_last_scenario(state, advisor_id=advisor_id)

    if action_id == ACTION_SKIP_RATING or action_id.startswith(ACTION_RATE_PREFIX):
        return await _handle_rating_action(
            state, advisor_id=advisor_id, action_id=action_id
        )

    raise CopilotFsmError(
        f"Unknown action '{action_id}'.",
        error_code=WBP_SCENARIO_CLIENT,
        http_status=400,
    )


__all__ = [
    "handle_action",
    "_handle_run",
    "_handle_update_holdings",
    "validate_import",
    "build_base",
    "set_portfolio_import_hitl",
    "parse_and_create_run",
    "execute_scenario_run",
    "place_all_staged_orders",
    "save_interaction_rating",
]
