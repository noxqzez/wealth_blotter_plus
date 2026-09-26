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
    classify_intent,
    redirect_for_intent,
    validate_layer1,
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

_BLOCKED_PARAM_KEYS = frozenset(
    {
        "coin_temperature",
        "trading_rules",
        "optimizer",
        "downstream_instructions",
        "source_agent",
        "message_type",
        "workflow_id",
    }
)

_TICKER_IN_ERROR_RE = re.compile(
    r"(?:Unsupported crypto|Invalid equity ticker|Incorrect asset class for|"
    r"Unsupported currency)\s+'?([A-Za-z0-9.\-]+)'?",
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
        label="Confirm Scenario Params",
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
    }
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
) -> CopilotAgentResponse:
    action_list = actions if actions is not None else _actions_for_ids(state.pending_action_ids)
    return CopilotAgentResponse(
        message_id=sessions.new_message_id(),
        session_id=state.session_id,
        current_state=state.current_state,
        content=content,
        guardrails=guardrails or [],
        actions=action_list,
        execution_status_badges=badges or [],
        excel_writes=excel_writes or [],
        download=download,
        accept_user_text=accept_user_text,
        user_text_placeholder=placeholder,
        scenario_params_summary=scenario_params_summary,
        portfolio_id=state.portfolio_id,
        scenario_run_id=state.scenario_run_id,
    )


# ---------------------------------------------------------------------------
# Excel write grids (mirror taskpane.ts / scenario.ts)
# ---------------------------------------------------------------------------


def _fmt_metric(n: float | None) -> float | str:
    if n is None:
        return ""
    return round(float(n), 4)


def _qty(n: float | None, asset_class: str | None) -> float | int | str:
    if n is None:
        return ""
    ac = (asset_class or "").upper()
    if ac in {"CASH", "CRYPTO"}:
        return round(float(n), 2)
    return int(float(n))


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
                p.quantity,
                p.short_name,
                round(float(p.spot_price), 2),
                p.market_value,
                round(float(p.current_weight_pct) * 100.0, 2),
                p.status,
                ts,
                str(p.pos_id),
            ]
        )
    return ExcelWriteSpec(
        sheet_name=SCRATCHPAD_SHEET,
        values=[header, *body],
        activate=True,
        hide_source_sheet=hide_source_sheet
        if hide_source_sheet and hide_source_sheet != SCRATCHPAD_SHEET
        else None,
        column_number_formats={
            "last_price": "#,##0.00",
            "nominal_value": "#,##0.00",
            "Pct weight (USD)": "0.00",
        },
    )


def build_rebalanced_write(
    body: ScenarioRunResponse,
    *,
    scenario_run_id: UUID,
) -> ExcelWriteSpec:
    positions = body.opti_folio or []
    points = body.talking_points or []
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
    rows: list[list[Any]] = [
        ["metrics", "base", "opti", "delta"],
    ]
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
    rows.append(
        [
            "ticker",
            "asset_class",
            "currency",
            "quantity",
            "nominal_value",
            "Pct weight (USD)",
        ]
    )
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
    rows.append([])
    rows.append(["talking_points"])
    for tp in points:
        rows.append([tp.bullet])

    width = max((len(r) for r in rows), default=1)
    padded = [r + [""] * (width - len(r)) for r in rows]
    return ExcelWriteSpec(
        sheet_name=REBALANCED_SHEET,
        values=padded,
        activate=True,
        column_number_formats={
            "nominal_value": "#,##0.00",
            "Pct weight (USD)": "0.00",
        },
    )


def build_ord_blotter_write(
    body: ScenarioRunResponse | Sequence[Any],
    *,
    scenario_run_id: UUID,
) -> ExcelWriteSpec:
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
    rows: list[list[Any]] = [
        [
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
        ],
    ]
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
        column_number_formats={},
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
        "(e.g. “lookback 3Y”, “risk aversion 4”) or confirm to continue.",
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

    # horizon
    hm = re.search(
        r"(?:investment\s+)?horizon\s*(?:to\s*|=\s*)?(\d+)\s*([ymwd])",
        lower,
    )
    if hm:
        scenario_patch["investment_horizon"] = f"{hm.group(1)}{hm.group(2).upper()}"

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

    # tone
    tone_map: dict[str, ToneLiteral] = {
        "risk off": "risk_off",
        "risk_off": "risk_off",
        "risk on": "risk_on",
        "risk_on": "risk_on",
        "neutral": "neutral",
    }
    for phrase, tone in tone_map.items():
        if re.search(rf"tone\s*(?:to\s*|=\s*)?{re.escape(phrase)}", lower) or (
            f"tone {phrase}" in lower
        ):
            scenario_patch["tone"] = tone
            break

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
        "or downstream_instructions.\n"
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
            "Welcome to Wealth Blotter Copilot.\n\n"
            "Select your securities list in Excel, then click "
            "**Process Security List** to validate the blotter."
        ),
        actions=[_action_process()],
        badges=["INIT"],
        accept_user_text=False,
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
        result = await validate_import(payload)
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
        return await _handle_confirm_params(state, advisor_id=advisor_id)

    if action_id == ACTION_RUN:
        return await _handle_run(state, advisor_id=advisor_id, limits=limits)

    if action_id == ACTION_PDF:
        return await _handle_pdf(state, advisor_id=advisor_id)

    if action_id == ACTION_PLACE:
        return await _handle_place_orders(state, advisor_id=advisor_id, request=request)

    raise CopilotFsmError(
        f"Unknown action '{action_id}'.",
        error_code=WBP_SCENARIO_CLIENT,
        http_status=400,
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
        badges=["PORTFOLIO_ENRICHED"],
        excel_writes=[write],
        accept_user_text=True,
        placeholder="Describe a market / macro scenario…",
    )


async def _handle_confirm_params(
    state: CopilotSessionState,
    *,
    advisor_id: UUID,
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
    state.pending_action_ids = [ACTION_RUN]
    # Stay SCENARIO_PARSED; free text off after confirm
    sessions.save_session(state)
    summary = scenario_params_to_summary(params)
    run_id = state.scenario_run_id
    return _response(
        state,
        content=(
            f"Scenario parameters confirmed for scenario `{run_id}`.\n\n"
            "Click **Run Optimization** to rebalance and stage orders."
        ),
        actions=[_action_run()],
        badges=["SCENARIO_PARSED"],
        accept_user_text=False,
        scenario_params_summary=summary,
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
        # Allow param edits + confirm, then retry run
        state.pending_action_ids = [ACTION_CONFIRM, ACTION_RUN]
        sessions.save_session(state)
        msg = result.error_message or "Optimization failed."
        return _response(
            state,
            content=f"Optimization did not complete: {msg}",
            actions=[_action_confirm(), _action_run()],
            guardrails=[
                GuardrailCheck(
                    rule_id="copilot.optimization_failed",
                    severity="blocking_error",
                    message=msg,
                    suggested_fix="Adjust parameters and try Run Optimization again.",
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
    state.pending_action_ids = [ACTION_PLACE, ACTION_PDF]
    sessions.save_session(state)

    n_trades = len(result.staged_trades or [])
    return _response(
        state,
        content=(
            f"Optimization complete. Wrote `{REBALANCED_SHEET}` and "
            f"`{ORD_BLOTTER_SHEET}` ({n_trades} staged order(s), status **Pending**).\n\n"
            "Review (and optionally edit) orders on `ord_blotter`, then "
            "**Place All Orders** to finish. **Download PDF** anytime."
        ),
        actions=[_action_place(), _action_pdf()],
        badges=["OPTIMIZATION_COMPLETE"],
        excel_writes=writes,
        accept_user_text=False,
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

    # Keep Place All available until orders are placed.
    if state.current_state == WorkflowState.ORDERS_PLACED:
        state.pending_action_ids = [ACTION_PDF]
        actions = [_action_pdf()]
        badges = ["ORDERS_PLACED", "PDF_READY"]
    else:
        state.pending_action_ids = [ACTION_PLACE, ACTION_PDF]
        actions = [_action_place(), _action_pdf()]
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
        accept_user_text=False,
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
    write = build_ord_blotter_write(orders, scenario_run_id=state.scenario_run_id)

    state.current_state = WorkflowState.ORDERS_PLACED
    state.pending_action_ids = [ACTION_PDF]
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
            "Workflow complete. Optional: **Download PDF**."
        ),
        actions=[_action_pdf()],
        badges=["ORDERS_PLACED"] + (["ORDERS_HITL"] if orders_hitl else []),
        excel_writes=[write],
        accept_user_text=False,
    )


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------


async def handle_message(
    *,
    session_id: str,
    advisor_id: UUID,
    text: str,
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

    # Free text only in PORTFOLIO_ENRICHED and SCENARIO_PARSED (pre-confirm)
    if state.current_state == WorkflowState.PORTFOLIO_ENRICHED:
        return await _message_scenario_prompt(state, cleaned, advisor_id=advisor_id)

    if state.current_state == WorkflowState.SCENARIO_PARSED:
        # After confirm, only Run Optimization is pending → redirect
        if state.pending_action_ids == [ACTION_RUN]:
            intent, checks = await classify_intent(
                cleaned, state=state.current_state
            )
            return _response(
                state,
                content=redirect_for_intent(intent),
                guardrails=checks,
                actions=[_action_run()],
                accept_user_text=False,
            )
        return await _message_param_edit(state, cleaned)

    intent, checks = await classify_intent(cleaned, state=state.current_state)
    if intent in {
        IntentClass.OFF_TOPIC_NONSENSE,
        IntentClass.PROMPT_INJECTION,
        IntentClass.WORKFLOW_COMMAND,
        IntentClass.FINANCIAL_INQUIRY,
        IntentClass.SCENARIO_PROMPT,
        IntentClass.PARAM_EDIT,
    }:
        return _response(
            state,
            content=redirect_for_intent(intent),
            guardrails=checks
            or [
                GuardrailCheck(
                    rule_id="copilot.state_gate",
                    severity="warning",
                    message=redirect_for_intent(intent),
                )
            ],
            accept_user_text=False,
            placeholder=None,
        )

    return _response(
        state,
        content=redirect_for_intent(IntentClass.OFF_TOPIC_NONSENSE),
        accept_user_text=False,
    )


def _accepts_user_text(state: CopilotSessionState) -> bool:
    if state.current_state == WorkflowState.PORTFOLIO_ENRICHED:
        return True
    if state.current_state == WorkflowState.SCENARIO_PARSED:
        return ACTION_CONFIRM in state.pending_action_ids
    return False


def _placeholder_for(state: CopilotSessionState) -> str | None:
    if state.current_state == WorkflowState.PORTFOLIO_ENRICHED:
        return "Describe a market / macro scenario…"
    if (
        state.current_state == WorkflowState.SCENARIO_PARSED
        and ACTION_CONFIRM in state.pending_action_ids
    ):
        return "Edit parameters or confirm…"
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
    state.pending_action_ids = [ACTION_CONFIRM]
    sessions.save_session(state)

    return _response(
        state,
        content=format_scenario_params_markdown(params),
        actions=[_action_confirm()],
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
    if intent == IntentClass.PROMPT_INJECTION or intent == IntentClass.OFF_TOPIC_NONSENSE:
        return _response(
            state,
            content=redirect_for_intent(intent),
            guardrails=checks,
            actions=[_action_confirm()],
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
                "or click **Confirm Scenario Params**."
            ),
            actions=[_action_confirm()],
            accept_user_text=True,
            placeholder="Edit parameters or confirm…",
            scenario_params_summary=scenario_params_to_summary(current),
        )

    state.scenario_params = patched.model_dump(mode="json")
    state.params_dirty = True
    state.pending_action_ids = [ACTION_CONFIRM]
    sessions.save_session(state)

    return _response(
        state,
        content=(
            "Updated draft parameters (not yet saved):\n\n"
            + format_scenario_params_markdown(patched)
        ),
        actions=[_action_confirm()],
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
