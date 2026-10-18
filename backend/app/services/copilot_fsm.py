"""Excel Copilot linear FSM: actions, messages, Excel write grids."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any, Sequence
from uuid import UUID

import structlog

from backend.app.agents.llm import build_llm
from backend.app.core.error_codes import (
    WBP_IMPORT_BUILD,
    WBP_IMPORT_CLIENT,
    WBP_IMPORT_VALIDATE,
    WBP_SCENARIO_CLIENT,
    WBP_SCENARIO_PARAMS,
    WBP_SCENARIO_PARSE,
    WBP_SCENARIO_PDF,
    WBP_SCENARIO_RUN,
    error_payload,
)
from backend.app.schemas.copilot import (
    ActionTrigger,
    ActionVariant,
    CopilotActionRequest,
    CopilotAgentResponse,
    CopilotSessionState,
    DownloadArtifact,
    ExcelWriteSpec,
    GuardrailCheck,
    IntentClass,
    WorkflowState,
    workflow_status_label,
)
from backend.app.schemas.portfolio_import import (
    BuildBaseRequest,
    BuildBaseResponse,
    EnrichedPosition,
    ImportValidationRequest,
    RawImportRow,
)
from backend.app.schemas.rules import TechnicalLimits
from backend.app.schemas.scenario import (
    ScenarioParams,
    ScenarioRunResponse,
    ToneLiteral,
)
from backend.app.services import copilot_session as sessions
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
from backend.app.services.interaction_ratings import (
    InteractionRatingError,
    has_rating_for_run,
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

SCRATCHPAD_SHEET = "portfolio_scratchpad"
REBALANCED_SHEET = "rebalanced_portfolio"
ORD_BLOTTER_SHEET = "ord_blotter"

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

_BLOCKED_PARAM_KEYS = frozenset(
    {
        "coin_temperature",
        "trading_rules",
        "optimizer",
        "downstream_instructions",
        "source_agent",
        "message_type",
        "workflow_id",
        # Domain identifiers are session/server-owned — chat is read-only.
        "portfolio_id",
        "scenario_run_id",
        "scenario_id",
        "order_id",
        "position_id",
        "pos_id",
        "draft_id",
    }
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


# ---------------------------------------------------------------------------
# Excel write grids (applied by frontend_excel/src/copilot.ts)
# ---------------------------------------------------------------------------


def _fmt_metric(n: float | None) -> float | str:
    if n is None:
        return ""
    return round(float(n), 4)


def _qty(n: float | None, asset_class: str | None) -> float | int | str:
    """Display qty always at 2dp so Excel #,##0.00 shows consistently."""
    if n is None:
        return ""
    _ = asset_class  # retained for call-site compatibility
    return round(float(n), 2)

# Light-blue header fill (matches enriched securities-list columns).
_HEADER_FILL_CYAN = "BDD7EE"


def _money(n: float | None) -> float | str:
    if n is None:
        return ""
    return round(float(n), 2)


def _pct(n: float | None) -> float | str:
    if n is None:
        return ""
    return round(float(n) * 100.0, 2)


def build_scratchpad_write(
    positions: list[EnrichedPosition],
    *,
    hide_source_sheet: str | None,
) -> ExcelWriteSpec:
    header = [
        "client_alias",
        "ticker",
        "asset_class",
        "currency",
        "quantity",
        "short_name",
        "last_price",
        "nominal_value",
        "Pct weight (USD)",
        "status",
        "price_timestamp",
        "pos_id",
    ]
    body: list[list[Any]] = []
    for p in positions:
        ts = p.price_timestamp.isoformat() if p.price_timestamp else ""
        body.append(
            [
                p.client_alias,
                p.ticker,
                p.asset_class,
                p.currency,
                # Always 2dp so Excel #,##0.00 shows "##.##" (not "##.").
                round(float(p.quantity), 2),
                p.short_name,
                round(float(p.spot_price), 2),
                round(float(p.market_value), 2) if p.market_value is not None else "",
                round(float(p.current_weight_pct) * 100.0, 2),
                p.status,
                ts,
                str(p.pos_id),
            ]
        )
    # Match advisor UX: gray = required paste fields; cyan = system enrichment.
    input_headers = (
        "client_alias",
        "ticker",
        "asset_class",
        "currency",
        "quantity",
    )
    enriched_headers = (
        "short_name",
        "last_price",
        "nominal_value",
        "Pct weight (USD)",
        "status",
        "price_timestamp",
        "pos_id",
    )
    header_fills = {
        **{h: "D9D9D9" for h in input_headers},
        **{h: "BDD7EE" for h in enriched_headers},
    }
    return ExcelWriteSpec(
        sheet_name=SCRATCHPAD_SHEET,
        values=[header, *body],
        activate=True,
        hide_source_sheet=hide_source_sheet
        if hide_source_sheet and hide_source_sheet != SCRATCHPAD_SHEET
        else None,
        column_number_formats={
            "quantity": "#,##0.00",
            "last_price": "#,##0.00",
            "nominal_value": "#,##0.00",
            "Pct weight (USD)": "0.00",
        },
        header_fill_colors=header_fills,
        header_bold=True,
        header_horizontal_alignment="center",
        table_border=True,
    )


def build_rebalanced_write(
    body: ScenarioRunResponse,
    *,
    scenario_run_id: UUID,
) -> ExcelWriteSpec:
    _ = scenario_run_id  # retained for call-site / audit correlation
    positions = body.opti_folio or []
    metric_keys = [
        "expected_return",
        "annualized_volatility",
        "sharpe_ratio",
        "sortino_ratio",
        "max_drawdown",
        "max_runup",
        "cvar_95",
        "scenario_return",
    ]
    metrics_header = ["metrics", "base", "opti", "delta"]
    rows: list[list[Any]] = [list(metrics_header)]
    base_m = body.base_client_metrics
    opti_m = body.opti_client_metrics
    delta = body.metrics_delta or {}
    for key in metric_keys:
        rows.append(
            [
                key,
                _fmt_metric(getattr(base_m, key, None) if base_m else None),
                _fmt_metric(getattr(opti_m, key, None) if opti_m else None),
                _fmt_metric(delta.get(key)),
            ]
        )
    rows.append([])
    rows.append(["opti_folio"])
    opti_header = [
        "ticker",
        "asset_class",
        "currency",
        "quantity",
        "nominal_value",
        "Pct weight (USD)",
    ]
    opti_header_row = len(rows)
    rows.append(list(opti_header))
    for p in positions:
        rows.append(
            [
                p.ticker,
                p.asset_class,
                p.currency or "",
                _qty(p.quantity, p.asset_class),
                _money(p.notional_value),
                _pct(p.weight_pct),
            ]
        )
    # Market commentary / talking points are PDF-only (simple + Gamma).

    width = max((len(r) for r in rows), default=1)
    padded = [r + [""] * (width - len(r)) for r in rows]
    header_fills = {
        **{h: _HEADER_FILL_CYAN for h in metrics_header},
        **{h: _HEADER_FILL_CYAN for h in opti_header},
    }
    return ExcelWriteSpec(
        sheet_name=REBALANCED_SHEET,
        values=padded,
        activate=True,
        column_number_formats={
            "base": "0.0000",
            "opti": "0.0000",
            "delta": "0.0000",
            "quantity": "#,##0.00",
            "nominal_value": "#,##0.00",
            "Pct weight (USD)": "0.00",
        },
        header_fill_colors=header_fills,
        header_row_indexes=[0, opti_header_row],
        header_bold=True,
        header_horizontal_alignment="center",
        table_border=True,
    )


def build_ord_blotter_write(
    body: ScenarioRunResponse | Sequence[Any],
    *,
    scenario_run_id: UUID,
) -> ExcelWriteSpec:
    _ = scenario_run_id  # retained for call-site / audit correlation
    if isinstance(body, ScenarioRunResponse):
        trades = list(body.staged_trades or [])
    else:
        trades = list(body)
    trades = sorted(
        trades,
        key=lambda t: (
            -(
                (t.to_weight_pct if getattr(t, "to_weight_pct", None) is not None else float("-inf"))
            ),
            str(t.ticker),
        ),
    )
    header = [
        "order_id",
        "client_alias",
        "Status",
        "ticker",
        "asset_class",
        "side",
        "quantity",
        "exec_type",
        "from_Pct weight (USD)",
        "to_Pct weight (USD)",
    ]
    rows: list[list[Any]] = [list(header)]
    for t in trades:
        rows.append(
            [
                t.order_id or "",
                getattr(t, "client_alias", None) or "",
                getattr(t, "status", None) or "Pending",
                t.ticker,
                t.asset_class,
                t.side,
                _qty(t.quantity, t.asset_class),
                getattr(t, "exec_type", None) or "Market",
                _pct(t.from_weight_pct),
                _pct(t.to_weight_pct),
            ]
        )
    width = max((len(r) for r in rows), default=1)
    padded = [r + [""] * (width - len(r)) for r in rows]
    return ExcelWriteSpec(
        sheet_name=ORD_BLOTTER_SHEET,
        values=padded,
        activate=False,
        column_number_formats={
            "quantity": "#,##0.00",
            "from_Pct weight (USD)": "0.00",
            "to_Pct weight (USD)": "0.00",
        },
        header_fill_colors={h: _HEADER_FILL_CYAN for h in header},
        header_row_indexes=[0],
        header_bold=True,
        header_horizontal_alignment="center",
        table_border=True,
    )


# ---------------------------------------------------------------------------
# Scenario params formatting / patching
# ---------------------------------------------------------------------------


def scenario_params_to_summary(params: ScenarioParams) -> dict[str, Any]:
    sc = params.scenario
    return {
        "scenario_name": sc.scenario_name,
        "scenario_type": sc.scenario_type,
        "severity": sc.severity,
        "tone": sc.tone,
        "investment_horizon": sc.investment_horizon,
        "lookback_window": sc.lookback_window,
        "risk_aversion": params.risk_aversion,
        "freeze_list": list(params.freeze_list),
        "weight_overrides": dict(params.weight_overrides),
        "macro_factors": list(params.detected_factors.macro_factors),
        "research_context_tags": list(params.research_context_tags),
    }


def format_scenario_params_markdown(params: ScenarioParams) -> str:
    s = scenario_params_to_summary(params)
    freeze = ", ".join(s["freeze_list"]) if s["freeze_list"] else "(none)"
    overrides = (
        ", ".join(f"{k}={v * 100:.1f}%" for k, v in s["weight_overrides"].items())
        if s["weight_overrides"]
        else "(none)"
    )
    factors = ", ".join(s["macro_factors"]) if s["macro_factors"] else "(none)"
    lines = [
        "### Scenario parameters",
        f"- **Name:** {s['scenario_name']}",
        f"- **Type:** {s['scenario_type']}",
        f"- **Tone:** {s['tone']}",
        f"- **Investment horizon:** {s['investment_horizon']}",
        f"- **Lookback window:** {s['lookback_window']}",
        f"- **Risk aversion:** {s['risk_aversion'] if s['risk_aversion'] is not None else 'default'}",
        f"- **Freeze list:** {freeze}",
        f"- **Weight overrides:** {overrides}",
        f"- **Macro factors:** {factors}",
        "",
        "Review the parameters above. You can type edits "
        "(e.g. “lookback 3Y”, “risk aversion 4”) or confirm to "
        "run optimization.",
    ]
    return "\n".join(lines)


def _apply_param_dict_patch(
    params: ScenarioParams,
    patch: dict[str, Any],
) -> ScenarioParams:
    """Apply a shallow/nested patch; reject blocked keys."""
    data = params.model_dump(mode="json")
    for key, value in patch.items():
        if key in _BLOCKED_PARAM_KEYS:
            continue
        if key == "scenario" and isinstance(value, dict):
            sc = dict(data.get("scenario") or {})
            for sk, sv in value.items():
                if sk in _BLOCKED_PARAM_KEYS:
                    continue
                sc[sk] = sv
            data["scenario"] = sc
        elif key == "detected_factors" and isinstance(value, dict):
            df = dict(data.get("detected_factors") or {})
            for fk, fv in value.items():
                if fk == "explicit_market_levels":
                    continue
                df[fk] = fv
            data["detected_factors"] = df
        elif key == "market_data_request_params" and isinstance(value, dict):
            mdr = dict(data.get("market_data_request_params") or {})
            for mk, mv in value.items():
                if mk in _BLOCKED_PARAM_KEYS:
                    continue
                mdr[mk] = mv
            data["market_data_request_params"] = mdr
        elif key in {
            "risk_aversion",
            "freeze_list",
            "weight_overrides",
            "research_context_tags",
            "user_prompt",
        }:
            data[key] = value
    # Never allow coin_temperature override via chat patch
    data["coin_temperature"] = params.coin_temperature
    return ScenarioParams.model_validate(data)


def _normalize_tone_token(raw: str) -> ToneLiteral | None:
    token = re.sub(r"[\s\-]+", "_", (raw or "").strip().lower())
    if token in {"risk_off", "riskoff", "off"}:
        return "risk_off"
    if token in {"risk_on", "riskon", "on"}:
        return "risk_on"
    if token == "neutral":
        return "neutral"
    return None


def _parse_tone_edit(lower: str) -> ToneLiteral | None:
    """Parse tone edits including 'change risk_off to risk_on' (no 'tone' required)."""
    # "change risk_off to risk_on" / "from risk off → risk on"
    m = re.search(
        r"(?:tone\s+)?(?:from\s+)?"
        r"(?:risk[_\s-]?off|risk[_\s-]?on|neutral)\s+"
        r"(?:to|->|→)\s*"
        r"(risk[_\s-]?off|risk[_\s-]?on|neutral)",
        lower,
    )
    if m:
        return _normalize_tone_token(m.group(1))
    # "tone risk_on" / "tone to risk on" / "set tone = risk_on"
    m = re.search(
        r"\btone\s*(?:to\s*|=\s*|:\s*)?(risk[_\s-]?off|risk[_\s-]?on|neutral)",
        lower,
    )
    if m:
        return _normalize_tone_token(m.group(1))
    # "make it risk_on" / "set risk on" / "use risk_on"
    m = re.search(
        r"\b(?:set|make(?:\s+it)?|use|switch(?:\s+to)?)\s+"
        r"(?:tone\s+(?:to\s+)?)?(risk[_\s-]?off|risk[_\s-]?on|neutral)\b",
        lower,
    )
    if m:
        return _normalize_tone_token(m.group(1))
    return None


def try_deterministic_param_patch(
    text: str,
    params: ScenarioParams,
) -> ScenarioParams | None:
    """Common phrase patches; return None if no match."""
    lower = text.lower().strip()
    patch: dict[str, Any] = {}
    scenario_patch: dict[str, Any] = {}

    # lookback (e.g. "lookback 3Y", "lookback window to 5y")
    lb = re.search(
        r"lookback\s*(?:window\s*)?(?:to\s*|=\s*)?(\d+)\s*([ym])",
        lower,
    )
    if lb:
        lookback = f"{lb.group(1)}{lb.group(2).upper()}"
        scenario_patch["lookback_window"] = lookback
        mdr = dict(params.market_data_request_params.model_dump())
        mdr["lookback_window"] = lookback
        patch["market_data_request_params"] = mdr

    # horizon (explicit "horizon 1Y" or temporal phrases)
    hm = re.search(
        r"(?:investment\s+)?horizon\s*(?:to\s*|=\s*)?(\d+)\s*([ymwd])",
        lower,
    )
    if hm:
        scenario_patch["investment_horizon"] = f"{hm.group(1)}{hm.group(2).upper()}"
    elif re.search(
        r"\b(?:next\s+year|over\s+the\s+next\s+year|by\s+next\s+year|"
        r"within\s+(?:a|one)\s+year|in\s+(?:a|one)\s+year)\b",
        lower,
    ):
        scenario_patch["investment_horizon"] = "1Y"
    elif (
        (m := re.search(
            r"\b(?:in|over|within|for)?\s*(?:the\s+)?next\s+(\d+)\s+months?\b",
            lower,
        ))
        and int(m.group(1)) >= 12
    ):
        scenario_patch["investment_horizon"] = "1Y"
    elif re.search(r"\b(?:by|in|through|until)\s+20\d{2}\b", lower) or re.search(
        r"\b20(2[6-9]|[3-9]\d)\b", lower
    ):
        # Future calendar year cue (2026+) → treat as ≥1Y planning horizon
        scenario_patch["investment_horizon"] = "1Y"
    elif re.search(r"\b(?:next\s+month|by\s+month[\s-]?end|this\s+month)\b", lower):
        scenario_patch["investment_horizon"] = "1M"
    elif re.search(r"\b(?:next\s+week|this\s+week)\b", lower):
        scenario_patch["investment_horizon"] = "1W"

    # severity
    for sev in ("low", "medium", "high"):
        if re.search(rf"severity\s*(?:to\s*|=\s*)?{sev}", lower) or (
            f"make it {sev}" in lower and "severity" in lower
        ):
            scenario_patch["severity"] = sev  # type: ignore[assignment]
            break
    if "severity high" in lower or "high severity" in lower:
        scenario_patch["severity"] = "high"
    if "severity low" in lower or "low severity" in lower:
        scenario_patch["severity"] = "low"
    if "severity medium" in lower or "medium severity" in lower:
        scenario_patch["severity"] = "medium"

    # tone — prefer explicit "X to Y" target so "change risk_off to risk_on" works
    parsed_tone = _parse_tone_edit(lower)
    if parsed_tone is not None:
        scenario_patch["tone"] = parsed_tone

    # risk aversion
    ram = re.search(
        r"risk[_\s-]?aversion\s*(?:to\s*|=\s*|λ\s*=\s*)?(\d+(?:\.\d+)?)",
        lower,
    )
    if ram:
        patch["risk_aversion"] = float(ram.group(1))

    # freeze list
    fm = re.search(
        r"freeze\s*(?:list\s*)?(?:to\s*|=\s*|:)?\s*([A-Za-z0-9.\-,\s]+)",
        lower,
    )
    if fm and "unfreeze" not in lower:
        raw = fm.group(1)
        tickers = [
            t.strip().upper()
            for t in re.split(r"[,;\s]+", raw)
            if t.strip() and re.fullmatch(r"[A-Z0-9.\-]{1,32}", t.strip().upper())
        ]
        # Stop at trailing words that aren't tickers
        tickers = [t for t in tickers if t not in {"TO", "AND", "THE", "LIST"}]
        if tickers:
            patch["freeze_list"] = tickers
    if re.search(r"clear\s+freeze|unfreeze\s+all|no\s+freeze", lower):
        patch["freeze_list"] = []

    if scenario_patch:
        patch["scenario"] = scenario_patch

    if not patch:
        return None
    try:
        return _apply_param_dict_patch(params, patch)
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot_param_patch_invalid", error=str(exc))
        return None


async def try_llm_param_patch(
    text: str,
    params: ScenarioParams,
) -> ScenarioParams | None:
    """Optional light LLM JSON patch; never touches blocked keys."""
    summary = scenario_params_to_summary(params)
    prompt = (
        "You patch scenario parameters for a wealth advisor Copilot.\n"
        "Return ONLY JSON with allowed fields among: "
        "scenario.scenario_name, scenario.scenario_type, scenario.severity "
        "(low|medium|high), scenario.tone (risk_off|risk_on|neutral), "
        "scenario.investment_horizon, scenario.lookback_window, "
        "risk_aversion (number), freeze_list (string[]), "
        "weight_overrides (object of ticker→fraction 0-1), "
        "research_context_tags (string[]), detected_factors.macro_factors.\n"
        "Do NOT change coin_temperature, trading rules, optimizer internals, "
        "downstream_instructions, portfolio_id, scenario_run_id, scenario_id, "
        "order_id, position_id, pos_id, draft_id, or workflow_id.\n"
        'Shape: {"patch": { ...fields... }}\n\n'
        f"Current params:\n{json.dumps(summary)}\n\n"
        f"Advisor edit request:\n{text}"
    )
    try:
        llm = build_llm(temperature=0.2)
        raw: Any = await asyncio.to_thread(llm.call, prompt)
        content = raw if isinstance(raw, str) else str(raw)
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        data = json.loads(content)
        patch = data.get("patch") if isinstance(data, dict) else None
        if not isinstance(patch, dict) or not patch:
            return None
        return _apply_param_dict_patch(params, patch)
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot_llm_param_patch_failed", error=str(exc))
        return None


# ---------------------------------------------------------------------------
# Welcome / create session
# ---------------------------------------------------------------------------


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
    "CopilotFsmError",
    "build_welcome_response",
    "handle_action",
    "handle_message",
    "build_scratchpad_write",
    "build_rebalanced_write",
    "build_ord_blotter_write",
]
