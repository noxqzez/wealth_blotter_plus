"""Shared Copilot FSM primitives: actions, errors, response builder."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import structlog

from backend.app.core.error_codes import error_payload
from backend.app.schemas.copilot import (
    ActionTrigger,
    ActionVariant,
    CopilotAgentResponse,
    CopilotSessionState,
    DownloadArtifact,
    ExcelWriteSpec,
    GuardrailCheck,
    WorkflowState,
    workflow_status_label,
)
from backend.app.services import copilot_session as sessions
from backend.app.services.interaction_ratings import has_rating_for_run

logger = structlog.get_logger(__name__)

ACTION_PROCESS = "process_security_list"
ACTION_REPROCESS = "reprocess_corrected_list"
ACTION_DROP = "drop_invalid_and_proceed"
ACTION_ENRICH = "enrich_portfolio"
ACTION_CONFIRM = "confirm_scenario_params"
ACTION_RUN = "run_optimization"
ACTION_PDF = "download_pdf"
ACTION_PLACE = "place_all_orders"
ACTION_SKIP_RATING = "skip_rating"
ACTION_NEW_SCENARIO = "run_new_scenario"
ACTION_UPDATE_HOLDINGS = "update_holdings"
ACTION_RATE_PREFIX = "rate_stars_"
_RATE_ACTION_IDS: tuple[str, ...] = tuple(
    f"{ACTION_RATE_PREFIX}{n}" for n in range(1, 6)
)


_TICKER_IN_ERROR_RE = re.compile(
    r"(?:Unsupported crypto|Invalid equity ticker|Incorrect asset class for|"
    r"Unsupported currency)\s+'?([A-Za-z0-9.\-]+)'?",
    re.IGNORECASE,
)

# Text restart → Update holdings (button equivalent). Kept distinct from
# ops "update prices" (which requires an explicit prices cue).
_HOLDINGS_RESTART_RE = re.compile(
    r"\b("
    r"update\s+holdings|"
    r"change\s+(the\s+)?(weights?|holdings|securities)|"
    r"add\s+(a\s+)?(security|securities|ticker|position)|"
    r"re-?import(\s+securities)?|"
    r"enrich(\s+the)?\s+portfolio(\s+selected)?|"
    r"process\s+(the\s+)?(selected\s+)?(securities|security\s+list)|"
    r"restart\s+(with\s+)?(new\s+)?holdings"
    r")\b",
    re.IGNORECASE,
)


class CopilotFsmError(Exception):
    """Domain error mapped to HTTP by the router."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        http_status: int = 400,
        support: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.http_status = http_status
        self.support = support

    def to_detail(self, correlation_id: str | None = None) -> dict[str, Any]:
        return error_payload(
            error_code=self.error_code,
            message=str(self),
            correlation_id=correlation_id,
            support=self.support,
        )


# ---------------------------------------------------------------------------
# Actions catalog helpers
# ---------------------------------------------------------------------------


def _action_process() -> ActionTrigger:
    return ActionTrigger(
        id=ACTION_PROCESS,
        label="Process Security List",
        variant=ActionVariant.PRIMARY,
        requires_excel_selection=True,
    )


def _action_reprocess() -> ActionTrigger:
    return ActionTrigger(
        id=ACTION_REPROCESS,
        label="Reprocess Corrected List",
        variant=ActionVariant.PRIMARY,
        requires_excel_selection=True,
    )


def _action_drop() -> ActionTrigger:
    return ActionTrigger(
        id=ACTION_DROP,
        label="Drop Invalid and Proceed",
        variant=ActionVariant.SECONDARY,
    )


def _action_enrich() -> ActionTrigger:
    return ActionTrigger(
        id=ACTION_ENRICH,
        label="Enrich Portfolio",
        variant=ActionVariant.PRIMARY,
    )


def _action_confirm() -> ActionTrigger:
    return ActionTrigger(
        id=ACTION_CONFIRM,
        label="Confirm & Run Optimization",
        variant=ActionVariant.PRIMARY,
    )


def _action_run() -> ActionTrigger:
    return ActionTrigger(
        id=ACTION_RUN,
        label="Run Optimization",
        variant=ActionVariant.PRIMARY,
    )


def _action_pdf() -> ActionTrigger:
    return ActionTrigger(
        id=ACTION_PDF,
        label="Download PDF",
        variant=ActionVariant.SECONDARY,
    )


def _action_place() -> ActionTrigger:
    return ActionTrigger(
        id=ACTION_PLACE,
        label="Place All Orders",
        variant=ActionVariant.PRIMARY,
    )


def _action_new_scenario() -> ActionTrigger:
    return ActionTrigger(
        id=ACTION_NEW_SCENARIO,
        label="Run new scenario",
        variant=ActionVariant.SECONDARY,
    )


def _action_update_holdings() -> ActionTrigger:
    return ActionTrigger(
        id=ACTION_UPDATE_HOLDINGS,
        label="Update holdings",
        variant=ActionVariant.SECONDARY,
        requires_excel_selection=False,
    )


def _action_rate(stars: int) -> ActionTrigger:
    """Server-side action id only — UI uses the compact star widget."""
    return ActionTrigger(
        id=f"{ACTION_RATE_PREFIX}{stars}",
        label=f"{stars}",
        variant=ActionVariant.SECONDARY,
    )


def _action_skip_rating() -> ActionTrigger:
    return ActionTrigger(
        id=ACTION_SKIP_RATING,
        label="Skip rating",
        variant=ActionVariant.SECONDARY,
    )


def _rating_pending(state: CopilotSessionState) -> bool:
    """True when we should still offer the optional post-opti star prompt."""
    if state.current_state != WorkflowState.OPTIMIZATION_COMPLETE:
        return False
    if state.scenario_run_id is None:
        return False
    return not (
        state.rating_captured and state.rating_for_run_id == state.scenario_run_id
    )


def _sync_rating_state(state: CopilotSessionState) -> None:
    """Align session rating flags with DB (one rating per simulation)."""
    run_id = state.scenario_run_id
    if run_id is None:
        return
    if state.rating_for_run_id != run_id:
        state.rating_for_run_id = run_id
        state.rating_captured = False
    if state.rating_captured:
        return
    if has_rating_for_run(scenario_run_id=run_id):
        state.rating_captured = True


def _post_opti_action_ids(state: CopilotSessionState) -> list[str]:
    ids: list[str] = []
    if _rating_pending(state):
        ids.extend(_RATE_ACTION_IDS)
        ids.append(ACTION_SKIP_RATING)
    # Place orders is the main next step; PDF / new scenario are secondary.
    # Update holdings is omitted here — restart via Process Security List instead.
    ids.extend(
        [
            ACTION_PLACE,
            ACTION_PDF,
            ACTION_NEW_SCENARIO,
        ]
    )
    return ids


def _post_opti_actions(state: CopilotSessionState) -> list[ActionTrigger]:
    """Visible chat buttons (stars rendered separately via show_star_rating)."""
    actions: list[ActionTrigger] = []
    if _rating_pending(state):
        actions.append(_action_skip_rating())
    actions.extend(
        [
            _action_place(),
            _action_pdf(),
            _action_new_scenario(),
        ]
    )
    return actions


def _orders_placed_action_ids() -> list[str]:
    # Terminal workflow: only restart via a fresh security-list import.
    return [ACTION_PROCESS]


def _orders_placed_actions() -> list[ActionTrigger]:
    return [_action_process()]


def _overlay_excel_order_qty(
    orders: Sequence[Any],
    excel_orders: Sequence[dict[str, Any]] | None,
) -> list[Any]:
    """Prefer advisor blotter quantities on the post-place Excel rewrite.

    DB staged qty stays as system-staged for audit; HITL is already flagged when
    they differ. Rewriting the sheet from DB alone looked like the edit "reverted".
    """
    if not excel_orders:
        return list(orders)
    by_id: dict[str, dict[str, Any]] = {}
    for raw in excel_orders:
        oid = str(raw.get("order_id") or "").strip()
        if oid:
            by_id[oid] = dict(raw)
    out: list[Any] = []
    for order in orders:
        oid = str(getattr(order, "order_id", "") or "")
        excel = by_id.get(oid)
        if excel is None:
            out.append(order)
            continue
        try:
            qty = float(excel.get("quantity"))
        except (TypeError, ValueError):
            out.append(order)
            continue
        if hasattr(order, "model_copy"):
            out.append(order.model_copy(update={"quantity": qty}))
        else:
            out.append(order)
    return out


def _review_scenario_actions() -> list[ActionTrigger]:
    return [_action_confirm(), _action_update_holdings()]


def _review_scenario_action_ids() -> list[str]:
    return [ACTION_CONFIRM, ACTION_UPDATE_HOLDINGS]


def _rating_prompt_markdown() -> str:
    return (
        "\n\n---\n"
        "Rate the simulation below (optional)"
    )


def _actions_for_ids(ids: list[str]) -> list[ActionTrigger]:
    catalog: dict[str, ActionTrigger] = {
        ACTION_PROCESS: _action_process(),
        ACTION_REPROCESS: _action_reprocess(),
        ACTION_DROP: _action_drop(),
        ACTION_ENRICH: _action_enrich(),
        ACTION_CONFIRM: _action_confirm(),
        ACTION_RUN: _action_run(),
        ACTION_PDF: _action_pdf(),
        ACTION_PLACE: _action_place(),
        ACTION_SKIP_RATING: _action_skip_rating(),
        ACTION_NEW_SCENARIO: _action_new_scenario(),
        ACTION_UPDATE_HOLDINGS: _action_update_holdings(),
    }
    for n in range(1, 6):
        catalog[f"{ACTION_RATE_PREFIX}{n}"] = _action_rate(n)
    return [catalog[i] for i in ids if i in catalog]


def _placeholder_for(state: CopilotSessionState) -> str | None:
    if state.current_state == WorkflowState.INIT:
        return "Ask a lookup, or process your securities list…"
    if state.current_state == WorkflowState.PORTFOLIO_ENRICHED:
        return "Describe a scenario, or ask a portfolio/PDF lookup…"
    if state.current_state == WorkflowState.SCENARIO_PARSED:
        if ACTION_CONFIRM in state.pending_action_ids:
            return "Edit parameters, confirm, or ask a lookup…"
        return "Ask a lookup, or continue the workflow…"
    if state.current_state == WorkflowState.OPTIMIZATION_COMPLETE:
        return (
            "Ask a lookup, place orders, PDF, new scenario, or update holdings…"
        )
    if state.current_state == WorkflowState.ORDERS_PLACED:
        return "Ask a lookup, or process a new securities list to restart…"
    return None


def _response(
    state: CopilotSessionState,
    *,
    content: str,
    actions: list[ActionTrigger] | None = None,
    guardrails: list[GuardrailCheck] | None = None,
    badges: list[str] | None = None,
    excel_writes: list[ExcelWriteSpec] | None = None,
    download: DownloadArtifact | None = None,
    accept_user_text: bool = False,
    placeholder: str | None = None,
    scenario_params_summary: dict[str, Any] | None = None,
    show_star_rating: bool | None = None,
) -> CopilotAgentResponse:
    action_list = actions if actions is not None else _actions_for_ids(state.pending_action_ids)
    # Never render individual rate_stars_* buttons — compact widget instead.
    action_list = [
        a for a in action_list if not a.id.startswith(ACTION_RATE_PREFIX)
    ]
    rating_ui = (
        bool(show_star_rating)
        if show_star_rating is not None
        else _rating_pending(state)
    )
    return CopilotAgentResponse(
        message_id=sessions.new_message_id(),
        session_id=state.session_id,
        current_state=state.current_state,
        status_label=workflow_status_label(state.current_state),
        content=content,
        guardrails=guardrails or [],
        actions=action_list,
        execution_status_badges=badges or [],
        excel_writes=excel_writes or [],
        download=download,
        accept_user_text=accept_user_text,
        user_text_placeholder=placeholder,
        show_star_rating=rating_ui,
        scenario_params_summary=scenario_params_summary,
        portfolio_id=state.portfolio_id,
        scenario_run_id=state.scenario_run_id,
    )


# Welcome / create session
# ---------------------------------------------------------------------------



