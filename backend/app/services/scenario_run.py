"""WF-02 Phase-1 scenario orchestration (parse / params / run / PDF helpers).

HTTP layer lives in ``api/v1/scenarios.py``. Timeouts (caller-enforced):
parse ~30s, params ~15s, run ~120s, PDF ~30s (see configs.scenario_wf02).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import structlog

from backend.app.agents.scenario_crew import (
    build_stub_strategy_view,
    coin_temperature_from_config,
    parse_scenario_prompt,
)
from backend.app.core.error_codes import (
    WBP_SCENARIO_BASE_REQUIRED,
    WBP_SCENARIO_CLIENT,
    WBP_SCENARIO_COMPLIANCE,
    WBP_SCENARIO_INVALID_PROMPT,
    WBP_SCENARIO_PARAMS,
    WBP_SCENARIO_PARSE,
    WBP_SCENARIO_PDF,
    WBP_SCENARIO_RUN,
    WBP_SCENARIO_SERVER,
    WBP_SCENARIO_TIMEOUT,
)
from backend.app.core.supabase import get_service_supabase
from backend.app.schemas.rules import TechnicalLimits
from backend.app.schemas.scenario import (
    ClientPortfolioMetrics,
    OptiPosition,
    PastAnalogueArtifact,
    ScenarioBlock,
    ScenarioParams,
    ScenarioParseResponse,
    ScenarioRunResponse,
    ScenarioUpdateParamsResponse,
    StagedTrade,
)
from backend.app.services.compliance_cite import load_active_trading_rules
from backend.app.services.trading_rules_policy import approved_crypto_bases_from_rules
from backend.app.services.market_fx import (
    currency_for_ticker,
    fetch_hkd_per_usd,
)
from backend.app.services.market_history import snapshot_scenario_market_prices
from backend.app.services.risk_engine import (
    compute_client_metrics_from_weights,
    metrics_delta,
)
from backend.app.services.scenario_audit import (
    STEP_COMPLIANCE,
    STEP_CONTENT,
    STEP_HOUSE_RESEARCH,
    STEP_MARKET_HISTORY,
    STEP_MARKET_RESEARCH,
    STEP_PARAMS_UPDATE,
    STEP_PARSE,
    STEP_QUANT,
    STEP_QUANT_SAFETY,
    STEP_RISK_BASE,
    STEP_PAST_ANALOGUE,
    STEP_RISK_OPTI,
    STEP_RUN_COMPLETE,
    STEP_RUN_FAILED,
    STEP_STRATEGIST,
    write_scenario_step_audit,
)
from backend.app.services.scenario_history import (
    ScenarioHistoryError,
    build_scenario_universe,
    fetch_scenario_history,
    parse_lookback_days,
)
from backend.app.services.scenario_optimizer import (
    DEFAULT_RISK_AVERSION,
    ComplianceUnresolvedError,
    ScenarioOptimizerError,
    propose_opti_folio,
)
from backend.app.services.scenario_param_diffs import diff_scenario_params
from backend.app.services.scenario_pdf import build_scenario_pdf
from backend.app.services.scenario_prompt_guard import (
    INVALID_PROMPT_MESSAGE,
    assert_scenario_prompt_in_scope,
)
from backend.app.services.scenario_research import (
    consolidate_strategy_view,
    run_house_research,
    run_market_research,
)
from backend.app.services.scenario_talking_points import (
    build_talking_points,
    extract_news_footnotes,
    format_news_footnote_lines,
)
from backend.app.services.scenario_performance import (
    DEFAULT_CHART_LOOKBACK_DAYS,
    DEFAULT_CHART_LOOKBACK_WINDOW,
    PerformanceBuildError,
    build_base_vs_opti_performance,
)
from backend.app.services.past_analogue import (
    past_analogue_apply_enabled,
    past_analogue_enabled,
    past_analogue_knobs,
    run_past_analogue_pipeline,
)
from backend.app.services.ticker_normalize import normalize_ticker as _normalize_ticker

logger = structlog.get_logger(__name__)

_SEED_CONFIG_DIR = Path(__file__).resolve().parent.parent / "db" / "seed_configs"
_MIGRATION_HINT = (
    "scenario_runs table missing — apply backend/app/db/migration_scenario_wf02.sql "
    "in the Supabase SQL Editor, then retry."
)
_QTY_EPS = 1e-6
_WEIGHT_EPS = 1e-8

# Soft timeout hints for HTTP layer (seconds). Enforced via asyncio.wait_for there.
PARSE_TIMEOUT_SECONDS = 120
PARAMS_TIMEOUT_SECONDS = 15
RUN_TIMEOUT_SECONDS = 120
PDF_TIMEOUT_SECONDS = 30


class ScenarioServiceError(Exception):
    """Domain error mapped to WBP-SCENARIO-* by the API layer."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = WBP_SCENARIO_SERVER,
        http_status: int = 500,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.http_status = http_status
        self.details = details or {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_missing_relation(exc: BaseException) -> bool:
    text = str(exc).lower()
    code = str(getattr(exc, "code", "") or "")
    if code in {"42P01", "PGRST205"}:
        return True
    scenario_tables = (
        "scenario_runs",
        "scenario_param_diffs",
        "staged_trades",
        "scenario_market_price",
    )
    if any(t in text for t in scenario_tables) and (
        "does not exist" in text
        or "could not find the table" in text
        or "pgrst205" in text
        or "schema cache" in text
    ):
        return True
    return False


def _guard_db(exc: BaseException) -> None:
    if _is_missing_relation(exc):
        raise ScenarioServiceError(
            _MIGRATION_HINT,
            error_code=WBP_SCENARIO_SERVER,
            http_status=503,
        ) from exc


def _client() -> Any:
    return get_service_supabase()


def _load_json_seed(name: str) -> dict[str, Any]:
    path = _SEED_CONFIG_DIR / name
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def _load_config_row(key: str) -> dict[str, Any]:
    try:
        result = _client().table("configs").select("config").eq("key", key).limit(1).execute()
        if result.data and isinstance(result.data[0].get("config"), dict):
            return dict(result.data[0]["config"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("scenario_config_load_failed", key=key, error=str(exc))
    return _load_json_seed(f"{key}.json")


def _position_maps(
    positions: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, float], dict[str, float], dict[str, str], float]:
    """Return weights, quantities, asset_classes, portfolio_value."""
    weights: dict[str, float] = {}
    quantities: dict[str, float] = {}
    asset_classes: dict[str, str] = {}
    total = 0.0
    for pos in positions:
        ticker = _normalize_ticker(str(pos.get("ticker", "")))
        if not ticker:
            continue
        ac = str(pos.get("asset_class", "EQUITY")).strip().upper() or "EQUITY"
        if ac == "CRYPTO":
            ticker = _normalize_ticker(ticker)
        qty = float(pos.get("quantity") or 0.0)
        notional = pos.get("notional_value")
        w = pos.get("current_weight_pct")
        quantities[ticker] = qty
        asset_classes[ticker] = ac
        if notional is not None:
            total += float(notional)
        if w is not None:
            weights[ticker] = float(w)
    if not weights and total > 0:
        for pos in positions:
            ticker = _normalize_ticker(str(pos.get("ticker", "")))
            notional = pos.get("notional_value")
            if ticker and notional is not None:
                weights[ticker] = float(notional) / total
    return weights, quantities, asset_classes, total


def _latest_prices_from_bars(bars: Sequence[Any]) -> dict[str, float]:
    by_ticker: dict[str, tuple[str, float]] = {}
    for bar in bars:
        ticker = _normalize_ticker(str(getattr(bar, "ticker", "")))
        price_date = str(getattr(bar, "price_date", ""))
        close = float(getattr(bar, "close_price", 0.0) or 0.0)
        if not ticker or close <= 0:
            continue
        prev = by_ticker.get(ticker)
        if prev is None or price_date >= prev[0]:
            by_ticker[ticker] = (price_date, close)
    return {t: px for t, (_d, px) in by_ticker.items()}


def _build_staged_trades(
    *,
    scenario_run_id: UUID,
    base_qty: Mapping[str, float],
    base_weights: Mapping[str, float],
    opti_positions: Sequence[Mapping[str, Any]],
    asset_classes: Mapping[str, str],
    client_alias: str | None = None,
) -> list[StagedTrade]:
    opti_by_ticker = {
        _normalize_ticker(str(p.get("ticker", ""))): p for p in opti_positions if p.get("ticker")
    }
    tickers = sorted(set(base_qty) | set(opti_by_ticker) | set(base_weights))
    trades: list[StagedTrade] = []
    seq = 0
    short = scenario_run_id.hex[:8].upper()
    alias = (client_alias or "").strip() or None
    for ticker in tickers:
        if ticker in {"CASH", "CASH_USD"}:
            continue
        bq = float(base_qty.get(ticker, 0.0))
        opti = opti_by_ticker.get(ticker, {})
        oq = float(opti.get("quantity") or 0.0)
        bw = float(base_weights.get(ticker, 0.0))
        ow = float(opti.get("weight_pct") or 0.0)
        dq = oq - bq
        if abs(dq) < _QTY_EPS and abs(ow - bw) < _WEIGHT_EPS:
            continue
        # Prefer quantity delta; fall back to weight-driven side
        if abs(dq) >= _QTY_EPS:
            side = "BUY" if dq > 0 else "SELL"
            qty = abs(dq)
        else:
            side = "BUY" if ow > bw else "SELL"
            qty = abs(ow - bw)
        ac_raw = str(
            opti.get("asset_class")
            or asset_classes.get(ticker)
            or ("CRYPTO" if ticker.endswith("-USD") else "EQUITY")
        ).upper()
        if ac_raw not in {"EQUITY", "CASH", "CRYPTO"}:
            ac_raw = "EQUITY"
        seq += 1
        notional = opti.get("notional_value")
        if notional is not None and bq > 0 and abs(dq) >= _QTY_EPS:
            # Approximate trade notional from opti unit value when possible
            unit = float(notional) / oq if oq else None
            notional_trade = float(unit * qty) if unit else None
        else:
            notional_trade = float(notional) if notional is not None and bq == 0 else None
        # Equity whole shares; crypto 2 dp (match opti_folio).
        if ac_raw == "CASH":
            qty_out = round(float(qty), 2)
        elif ac_raw == "CRYPTO":
            qty_out = round(float(qty), 2)
        else:
            qty_out = float(int(qty))
        if qty_out <= 0 and ac_raw != "CASH":
            continue
        trades.append(
            StagedTrade(
                order_id=f"ST-{short}-{ticker}-{side}-{seq:03d}",
                client_alias=alias,
                status="Pending",
                ticker=ticker,
                asset_class=ac_raw,  # type: ignore[arg-type]
                side=side,  # type: ignore[arg-type]
                quantity=qty_out,
                exec_type="Market",
                notional_value=(
                    round(float(notional_trade), 2)
                    if notional_trade is not None
                    else None
                ),
                from_weight_pct=round(float(bw), 6),
                to_weight_pct=round(float(ow), 6),
            )
        )
    return trades


def _persist_staged_trades(
    *,
    scenario_run_id: UUID,
    advisor_id: UUID,
    portfolio_id: UUID,
    trades: Sequence[StagedTrade],
    client_alias: str | None = None,
) -> None:
    if not trades:
        return
    client = _client()
    # Replace prior staged rows for this run (re-run safe)
    try:
        client.table("staged_trades").delete().eq(
            "scenario_run_id", str(scenario_run_id)
        ).execute()
    except Exception as exc:  # noqa: BLE001
        _guard_db(exc)
        raise
    alias = (client_alias or "").strip() or None
    rows = [
        {
            "order_id": t.order_id,
            "scenario_run_id": str(scenario_run_id),
            "advisor_id": str(advisor_id),
            "portfolio_id": str(portfolio_id),
            "client_alias": t.client_alias or alias,
            "status": t.status or "Pending",
            "ticker": t.ticker,
            "asset_class": t.asset_class,
            "side": t.side,
            "quantity": t.quantity,
            "exec_type": t.exec_type or "Market",
            "notional_value": t.notional_value,
            "from_weight_pct": t.from_weight_pct,
            "to_weight_pct": t.to_weight_pct,
            "metadata": {},
        }
        for t in trades
    ]
    try:
        client.table("staged_trades").insert(rows).execute()
    except Exception as exc:  # noqa: BLE001
        _guard_db(exc)
        raise


def place_all_staged_orders(
    *,
    scenario_run_id: UUID,
    advisor_id: UUID,
    excel_orders: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Dummy OMS: mark staged orders Executed; flag orders_hitl on blotter edits.

    Compares Excel ``ord_blotter`` rows (if provided) to DB staged trades on
    order_id / ticker / side / quantity. Any mismatch → ``scenario_runs.orders_hitl``.
    """
    client = _client()
    try:
        staged_res = (
            client.table("staged_trades")
            .select(
                "order_id,client_alias,status,ticker,asset_class,side,quantity,"
                "exec_type,from_weight_pct,to_weight_pct,notional_value"
            )
            .eq("scenario_run_id", str(scenario_run_id))
            .eq("advisor_id", str(advisor_id))
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        _guard_db(exc)
        raise

    staged_rows = list(staged_res.data or [])
    if not staged_rows:
        raise ScenarioServiceError(
            "No staged orders to place for this scenario run.",
            error_code=WBP_SCENARIO_CLIENT,
            http_status=400,
        )

    by_id = {str(r["order_id"]): r for r in staged_rows}
    orders_hitl = False
    mismatch_notes: list[str] = []

    if excel_orders is not None:
        excel_by_id: dict[str, Mapping[str, Any]] = {}
        for raw in excel_orders:
            oid = str(raw.get("order_id") or "").strip()
            if not oid:
                continue
            excel_by_id[oid] = raw

        if set(excel_by_id) != set(by_id):
            orders_hitl = True
            missing = sorted(set(by_id) - set(excel_by_id))
            extra = sorted(set(excel_by_id) - set(by_id))
            if missing:
                mismatch_notes.append(f"missing from blotter: {', '.join(missing[:5])}")
            if extra:
                mismatch_notes.append(f"extra on blotter: {', '.join(extra[:5])}")

        for oid, db_row in by_id.items():
            excel = excel_by_id.get(oid)
            if excel is None:
                continue
            db_ticker = str(db_row.get("ticker") or "").upper()
            ex_ticker = str(excel.get("ticker") or "").strip().upper()
            db_side = str(db_row.get("side") or "").upper()
            ex_side = str(excel.get("side") or "").strip().upper()
            try:
                db_qty = float(db_row.get("quantity") or 0.0)
            except (TypeError, ValueError):
                db_qty = 0.0
            try:
                ex_qty = float(excel.get("quantity") or 0.0)
            except (TypeError, ValueError):
                ex_qty = 0.0
            if ex_ticker != db_ticker or ex_side != db_side or abs(ex_qty - db_qty) > 1e-6:
                orders_hitl = True
                mismatch_notes.append(
                    f"{oid}: staged {db_side} {db_qty} {db_ticker} "
                    f"≠ blotter {ex_side} {ex_qty} {ex_ticker}"
                )

    now = datetime.now(timezone.utc).isoformat()
    try:
        client.table("staged_trades").update(
            {"status": "Executed"}
        ).eq("scenario_run_id", str(scenario_run_id)).eq(
            "advisor_id", str(advisor_id)
        ).execute()
        client.table("scenario_runs").update(
            {"orders_hitl": bool(orders_hitl), "updated_at": now}
        ).eq("id", str(scenario_run_id)).execute()
    except Exception as exc:  # noqa: BLE001
        _guard_db(exc)
        raise

    executed: list[StagedTrade] = []
    for r in staged_rows:
        executed.append(
            StagedTrade(
                order_id=str(r["order_id"]),
                client_alias=r.get("client_alias"),
                status="Executed",
                ticker=str(r["ticker"]),
                asset_class=str(r["asset_class"]),  # type: ignore[arg-type]
                side=str(r["side"]),  # type: ignore[arg-type]
                quantity=float(r["quantity"]),
                exec_type="Market",
                notional_value=(
                    float(r["notional_value"])
                    if r.get("notional_value") is not None
                    else None
                ),
                from_weight_pct=(
                    float(r["from_weight_pct"])
                    if r.get("from_weight_pct") is not None
                    else None
                ),
                to_weight_pct=(
                    float(r["to_weight_pct"])
                    if r.get("to_weight_pct") is not None
                    else None
                ),
            )
        )

    logger.info(
        "place_all_staged_orders",
        scenario_run_id=str(scenario_run_id),
        n_orders=len(executed),
        orders_hitl=orders_hitl,
        mismatches=len(mismatch_notes),
    )
    return {
        "orders": executed,
        "orders_hitl": orders_hitl,
        "mismatch_notes": mismatch_notes[:20],
        "n_orders": len(executed),
    }


def _load_portfolio_for_scenario(portfolio_id: UUID) -> dict[str, Any]:
    try:
        result = (
            _client()
            .table("portfolios")
            .select("id, client_id, folio_stage, cash_balance, currency")
            .eq("id", str(portfolio_id))
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        _guard_db(exc)
        raise ScenarioServiceError(
            f"Failed to load portfolio: {exc}",
            error_code=WBP_SCENARIO_SERVER,
        ) from exc
    if not result.data:
        raise ScenarioServiceError(
            f"Portfolio {portfolio_id} not found",
            error_code=WBP_SCENARIO_CLIENT,
            http_status=404,
        )
    row = result.data[0]
    if str(row.get("folio_stage") or "").upper() != "BASE":
        raise ScenarioServiceError(
            "Scenario requires portfolio folio_stage=BASE (complete WF-01 build-base first)",
            error_code=WBP_SCENARIO_BASE_REQUIRED,
            http_status=400,
        )
    return row


def _load_client_alias(client_id: str | None) -> str | None:
    if not client_id:
        return None
    try:
        result = (
            _client()
            .table("clients")
            .select("client_alias")
            .eq("id", str(client_id))
            .limit(1)
            .execute()
        )
        if result.data:
            return str(result.data[0].get("client_alias") or "") or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("client_alias_load_failed", error=str(exc))
    return None


def _load_base_positions(portfolio_id: UUID) -> list[dict[str, Any]]:
    try:
        result = (
            _client()
            .table("portfolio_positions")
            .select(
                "id, ticker, asset_class, quantity, spot_price, notional_value, "
                "current_weight_pct, currency"
            )
            .eq("portfolio_id", str(portfolio_id))
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        _guard_db(exc)
        raise ScenarioServiceError(
            f"Failed to load base positions: {exc}",
            error_code=WBP_SCENARIO_SERVER,
        ) from exc
    rows = list(result.data or [])
    if not rows:
        raise ScenarioServiceError(
            "Portfolio has no base positions",
            error_code=WBP_SCENARIO_BASE_REQUIRED,
            http_status=400,
        )
    # Normalize tickers for downstream services
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["ticker"] = _normalize_ticker(str(item.get("ticker", "")))
        item["asset_class"] = str(item.get("asset_class", "EQUITY")).strip().upper()
        out.append(item)
    return out


def _load_scenario_run(scenario_run_id: UUID, *, advisor_id: UUID | None = None) -> dict[str, Any]:
    try:
        q = (
            _client()
            .table("scenario_runs")
            .select("*")
            .eq("id", str(scenario_run_id))
            .limit(1)
        )
        if advisor_id is not None:
            q = q.eq("advisor_id", str(advisor_id))
        result = q.execute()
    except Exception as exc:  # noqa: BLE001
        _guard_db(exc)
        raise ScenarioServiceError(
            f"Failed to load scenario_run: {exc}",
            error_code=WBP_SCENARIO_SERVER,
        ) from exc
    if not result.data:
        raise ScenarioServiceError(
            f"scenario_run {scenario_run_id} not found",
            error_code=WBP_SCENARIO_CLIENT,
            http_status=404,
        )
    return result.data[0]


def _params_from_row(raw: Any) -> ScenarioParams:
    if isinstance(raw, ScenarioParams):
        return raw
    if not isinstance(raw, dict):
        raise ScenarioServiceError(
            "scenario_params missing or invalid on scenario_run",
            error_code=WBP_SCENARIO_PARAMS,
            http_status=400,
        )
    return ScenarioParams.model_validate(raw)


async def parse_and_create_run(
    *,
    portfolio_id: UUID,
    user_prompt: str,
    advisor_id: UUID,
) -> ScenarioParseResponse:
    """Create scenario_runs row (PARSED) after NL → ScenarioParams."""
    portfolio = _load_portfolio_for_scenario(portfolio_id)
    scenario_cfg = _load_config_row("scenario_wf02")
    phase = str(scenario_cfg.get("phase") or "phase1")
    if phase not in {"phase1", "phase2"}:
        phase = "phase1"
    feature_flags = scenario_cfg.get("feature_flags") or {}
    if not isinstance(feature_flags, dict):
        feature_flags = {}

    try:
        assert_scenario_prompt_in_scope(user_prompt)
    except ValueError as exc:
        raise ScenarioServiceError(
            str(exc) or INVALID_PROMPT_MESSAGE,
            error_code=WBP_SCENARIO_INVALID_PROMPT,
            http_status=400,
        ) from exc

    try:
        params = await parse_scenario_prompt(user_prompt)
    except Exception as exc:  # noqa: BLE001
        logger.exception("scenario_parse_failed", error=str(exc))
        raise ScenarioServiceError(
            f"Scenario parse failed: {exc}",
            error_code=WBP_SCENARIO_PARSE,
            http_status=400,
        ) from exc

    run_id = uuid4()
    params = params.model_copy(update={"workflow_id": str(run_id)})
    payload = {
        "id": str(run_id),
        "advisor_id": str(advisor_id),
        "portfolio_id": str(portfolio_id),
        "client_id": portfolio.get("client_id"),
        "status": "PARSED",
        "phase": phase,
        "user_prompt": user_prompt.strip(),
        "agent_scenario_params": params.model_dump(mode="json"),
        "scenario_params": params.model_dump(mode="json"),
        "scenario_params_version": 1,
        "feature_flags": feature_flags,
        "updated_at": _now_iso(),
    }
    try:
        _client().table("scenario_runs").insert(payload).execute()
    except Exception as exc:  # noqa: BLE001
        _guard_db(exc)
        raise ScenarioServiceError(
            f"Failed to persist scenario_run: {exc}",
            error_code=WBP_SCENARIO_SERVER,
        ) from exc

    client_alias = _load_client_alias(
        str(portfolio["client_id"]) if portfolio.get("client_id") else None
    )
    write_scenario_step_audit(
        scenario_run_id=run_id,
        portfolio_id=portfolio_id,
        step=STEP_PARSE,
        agent_or_component="scenario_agent",
        advisor_id=advisor_id,
        client_alias=client_alias,
        phase=phase,
        implementation=str(params.source_agent or "scenario_agent"),
        input_payload={"user_prompt": user_prompt.strip()},
        output_payload={"scenario_params": params.model_dump(mode="json")},
        messages=["Scenario parameters parsed; review or edit before run."],
    )

    logger.info(
        "scenario_parsed",
        scenario_run_id=str(run_id),
        portfolio_id=str(portfolio_id),
        scenario_type=params.scenario.scenario_type,
    )
    return ScenarioParseResponse(
        scenario_run_id=run_id,
        portfolio_id=portfolio_id,
        status="PARSED",
        phase=phase,  # type: ignore[arg-type]
        agent_scenario_params=params,
        scenario_params=params,
        messages=["Scenario parameters parsed; review or edit before run."],
    )


async def update_scenario_params(
    *,
    scenario_run_id: UUID,
    human_params: ScenarioParams,
    advisor_id: UUID,
) -> ScenarioUpdateParamsResponse:
    """Diff agent vs human params, bump version, status PARAMS_UPDATED."""
    row = _load_scenario_run(scenario_run_id, advisor_id=advisor_id)
    try:
        agent_params = _params_from_row(row.get("agent_scenario_params"))
    except ScenarioServiceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ScenarioServiceError(
            f"Invalid agent_scenario_params: {exc}",
            error_code=WBP_SCENARIO_PARAMS,
            http_status=400,
        ) from exc

    diffs, is_exact = diff_scenario_params(agent_params, human_params)
    version = int(row.get("scenario_params_version") or 1) + 1
    now = _now_iso()

    if diffs:
        diff_rows = [
            {
                "scenario_run_id": str(scenario_run_id),
                "advisor_id": str(advisor_id),
                "field_path": d.field_path,
                "agent_value": d.agent_value,
                "human_value": d.human_value,
                "is_exact_match": d.is_exact_match,
            }
            for d in diffs
        ]
        try:
            _client().table("scenario_param_diffs").insert(diff_rows).execute()
        except Exception as exc:  # noqa: BLE001
            _guard_db(exc)
            raise ScenarioServiceError(
                f"Failed to persist scenario_param_diffs: {exc}",
                error_code=WBP_SCENARIO_SERVER,
            ) from exc

    try:
        _client().table("scenario_runs").update(
            {
                "scenario_params": human_params.model_dump(mode="json"),
                "scenario_params_version": version,
                "status": "PARAMS_UPDATED",
                "updated_at": now,
                "error_code": None,
                "error_message": None,
            }
        ).eq("id", str(scenario_run_id)).execute()
    except Exception as exc:  # noqa: BLE001
        _guard_db(exc)
        raise ScenarioServiceError(
            f"Failed to update scenario_params: {exc}",
            error_code=WBP_SCENARIO_SERVER,
        ) from exc

    logger.info(
        "scenario_params_updated",
        scenario_run_id=str(scenario_run_id),
        version=version,
        diff_count=len(diffs),
        is_exact_match=is_exact,
    )
    write_scenario_step_audit(
        scenario_run_id=scenario_run_id,
        portfolio_id=UUID(str(row["portfolio_id"])),
        step=STEP_PARAMS_UPDATE,
        agent_or_component="human_advisor",
        advisor_id=advisor_id,
        phase=str(row.get("phase") or "phase1"),
        implementation="human",
        input_payload={
            "agent_scenario_params": agent_params.model_dump(mode="json"),
        },
        output_payload={
            "scenario_params": human_params.model_dump(mode="json"),
            "scenario_params_version": version,
            "is_exact_match": is_exact,
            "diffs": [d.model_dump(mode="json") for d in diffs],
        },
    )
    return ScenarioUpdateParamsResponse(
        scenario_run_id=scenario_run_id,
        status="PARAMS_UPDATED",
        scenario_params=human_params,
        scenario_params_version=version,
        diffs=diffs,
        is_exact_match=is_exact,
    )


def mark_run_failed(
    scenario_run_id: UUID,
    *,
    error_code: str,
    error_message: str,
    portfolio_id: UUID | None = None,
    advisor_id: UUID | None = None,
) -> None:
    try:
        _client().table("scenario_runs").update(
            {
                "status": "FAILED",
                "error_code": error_code,
                "error_message": error_message[:2000],
                "updated_at": _now_iso(),
            }
        ).eq("id", str(scenario_run_id)).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "scenario_run_fail_persist_error",
            scenario_run_id=str(scenario_run_id),
            error=str(exc),
        )
    if portfolio_id is not None:
        write_scenario_step_audit(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            step=STEP_RUN_FAILED,
            agent_or_component="scenario_orchestrator",
            advisor_id=advisor_id,
            implementation="application",
            ok=False,
            input_payload={},
            output_payload={
                "status": "FAILED",
                "error_code": error_code,
                "error_message": error_message[:2000],
            },
        )


def _execute_phase1_sync(
    *,
    scenario_run_id: UUID,
    advisor_id: UUID,
    portfolio_id: UUID,
    phase: str,
    scenario_params: ScenarioParams,
    client_alias: str | None,
    trading_rules: Sequence[Mapping[str, Any]],
    limits: TechnicalLimits | None,
    house_research: Mapping[str, Any],
    market_research: Mapping[str, Any],
    feature_flags: Mapping[str, Any],
) -> ScenarioRunResponse:
    """Blocking Phase-1 core (Yahoo + risk + optimize). Called via to_thread."""
    messages: list[str] = []
    audit_kw: dict[str, Any] = {
        "scenario_run_id": scenario_run_id,
        "portfolio_id": portfolio_id,
        "advisor_id": advisor_id,
        "client_alias": client_alias,
        "phase": phase,
    }
    base_positions = _load_base_positions(portfolio_id)
    base_weights, base_qty, base_ac, portfolio_value = _position_maps(base_positions)

    approved = approved_crypto_bases_from_rules(trading_rules)
    from backend.app.services.coin_temperature import normalize_coin_temperature

    # Management crypto dial: configs.business_guardrails (Streamlit later).
    # Optional per-run override only when ScenarioParams.coin_temperature is set.
    raw_coin = getattr(scenario_params, "coin_temperature", None)
    if raw_coin is not None:
        coin_temp = normalize_coin_temperature(raw_coin)
    else:
        coin_temp = coin_temperature_from_config(
            _load_config_row("business_guardrails")
        )

    universe = build_scenario_universe(
        base_positions, approved_crypto=[str(c) for c in approved]
    )
    lookback = parse_lookback_days(
        scenario_params.scenario.lookback_window
        or scenario_params.market_data_request_params.lookback_window
        or DEFAULT_CHART_LOOKBACK_WINDOW
    )
    # Always fetch at least the PDF chart window (5Y) so performance series
    # stays full-length even if the scenario/optimizer window is shorter.
    lookback = max(int(lookback), int(DEFAULT_CHART_LOOKBACK_DAYS))
    scenario_cfg = _load_config_row("scenario_wf02")
    # Past Analogue (flagged off by default): extend panel to archive length.
    if past_analogue_enabled(feature_flags, scenario_cfg):
        knobs_pre = past_analogue_knobs(scenario_cfg)
        lookback = max(int(lookback), int(knobs_pre.get("archive_days") or 2920))
    history = fetch_scenario_history(
        universe, lookback_days=lookback, limits=limits
    )
    # Keep cash-proxy skips in audit only — not advisor-facing warnings.
    messages.extend(
        m
        for m in history.messages
        if "Skipped cash proxy" not in m
    )

    write_scenario_step_audit(
        **audit_kw,
        step=STEP_MARKET_HISTORY,
        agent_or_component="market_history_service",
        implementation="yahoo_eod",
        input_payload={
            "universe": [{"ticker": t, "asset_class": ac} for t, ac in universe],
            "lookback_days": lookback,
            "scenario_params": scenario_params.model_dump(mode="json"),
        },
        output_payload={
            "n_price_rows": int(len(history.prices)),
            "n_return_rows": int(len(history.returns)),
            "tickers": list(history.prices.columns.astype(str)),
            "provider_symbols": history.provider_symbols,
            "bar_count": len(history.bars),
        },
        messages=list(history.messages),
    )

    try:
        snapshot_scenario_market_prices(
            scenario_run_id=scenario_run_id,
            bars=history.bars,
            advisor_id=advisor_id,
            also_upsert_canonical=True,
        )
    except Exception as snap_exc:  # noqa: BLE001
        if _is_missing_relation(snap_exc):
            raise ScenarioServiceError(
                _MIGRATION_HINT,
                error_code=WBP_SCENARIO_SERVER,
                http_status=503,
            ) from snap_exc
        messages.append(f"Market price snapshot skipped: {snap_exc}")
        logger.warning(
            "scenario_price_snapshot_failed",
            scenario_run_id=str(scenario_run_id),
            error=str(snap_exc),
        )

    # House / market research payloads produced before this sync block.
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_HOUSE_RESEARCH,
        agent_or_component="house_research_agent",
        implementation=str(house_research.get("implementation") or "stub"),
        input_payload={
            "scenario_params": scenario_params.model_dump(mode="json"),
            "universe_tickers": [t for t, _ in universe],
        },
        output_payload=dict(house_research),
        messages=list(house_research.get("messages") or []),
    )
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_MARKET_RESEARCH,
        agent_or_component="market_research_agent",
        implementation=str(market_research.get("implementation") or "stub"),
        input_payload={
            "scenario_params": scenario_params.model_dump(mode="json"),
            "universe_tickers": [t for t, _ in universe],
        },
        output_payload=dict(market_research),
        messages=list(market_research.get("messages") or []),
    )

    use_stub_strategist = bool(feature_flags.get("strategist_stub_ok", False))
    if use_stub_strategist:
        strategy_view = build_stub_strategy_view(
            universe_tickers=[t for t, _ac in universe],
            coin_temperature=coin_temp,
            scenario_tone=scenario_params.scenario.tone,
            user_prompt=scenario_params.user_prompt or "",
        )
        strategist_impl = "stub"
    else:
        strategy_view = consolidate_strategy_view(
            house_research=house_research,
            market_research=market_research,
            universe_tickers=[t for t, _ac in universe],
            coin_temperature=coin_temp,
            scenario_tone=scenario_params.scenario.tone,
        )
        strategist_impl = "in_house_priority_merge"
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_STRATEGIST,
        agent_or_component="strategist_agent",
        implementation=strategist_impl,
        input_payload={
            "house_research": {
                "implementation": house_research.get("implementation"),
                "hit_count": house_research.get("hit_count"),
                "asset_views": house_research.get("asset_views"),
            },
            "market_research": {
                "implementation": market_research.get("implementation"),
                "article_count": market_research.get("article_count"),
                "asset_views": market_research.get("asset_views"),
            },
            "coin_temperature": coin_temp,
            "scenario_tone": scenario_params.scenario.tone,
        },
        output_payload={"strategy_view": strategy_view.model_dump(mode="json")},
        messages=list(strategy_view.notes),
    )

    base_metrics = compute_client_metrics_from_weights(history.returns, base_weights)
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_RISK_BASE,
        agent_or_component="risk_engine",
        implementation="deterministic",
        input_payload={"weights": base_weights, "n_return_rows": int(len(history.returns))},
        output_payload={"base_client_metrics": base_metrics.model_dump(mode="json")},
    )

    prices = _latest_prices_from_bars(history.bars)
    # Keep local last prices (HKD for *.HK). Prefer BASE blotter spots when present.
    ticker_ccy: dict[str, str] = {}
    for pos in base_positions:
        t = _normalize_ticker(str(pos.get("ticker", "")))
        if not t:
            continue
        ticker_ccy[t] = currency_for_ticker(
            t, str(pos.get("currency") or "") or None
        )
        spot = pos.get("spot_price")
        if spot is not None and float(spot) > 0:
            prices[t] = float(spot)

    hkd_per_usd = fetch_hkd_per_usd(limits=limits)
    messages.append(f"FX HKD=X={hkd_per_usd:.6f} (USD notionals only; last price local)")

    quant_input = {
        "strategy_view": strategy_view.model_dump(mode="json"),
        "base_positions": [
            {
                "ticker": p.get("ticker"),
                "asset_class": p.get("asset_class"),
                "quantity": p.get("quantity"),
                "current_weight_pct": p.get("current_weight_pct"),
                "spot_price": p.get("spot_price"),
                "currency": p.get("currency"),
                "notional_value": p.get("notional_value"),
            }
            for p in base_positions
        ],
        "trading_rules": [
            {
                "rule_name": r.get("rule_name"),
                "rule_category": r.get("rule_category"),
                "parameters": r.get("parameters"),
                "is_active": r.get("is_active"),
            }
            for r in trading_rules
        ],
        "portfolio_value": portfolio_value,
        "price_tickers": sorted(prices.keys()),
        "hkd_per_usd": hkd_per_usd,
        "n_return_rows": int(len(history.returns)),
    }

    opti = propose_opti_folio(
        returns=history.returns,
        base_positions=base_positions,
        strategy_view=strategy_view,
        trading_rules=list(trading_rules),
        portfolio_value=portfolio_value or None,
        prices=prices,
        ticker_currency=ticker_ccy,
        hkd_per_usd=hkd_per_usd,
        client_alias=client_alias,
        risk_aversion=float(
            scenario_params.risk_aversion
            if scenario_params.risk_aversion is not None
            else DEFAULT_RISK_AVERSION
        ),
        freeze_list=list(scenario_params.freeze_list or []),
        weight_overrides=dict(scenario_params.weight_overrides or {}),
    )
    messages.extend(list(opti.get("messages") or []))

    opti_weights: dict[str, float] = {
        _normalize_ticker(str(k)): float(v)
        for k, v in (opti.get("weights") or {}).items()
    }
    safety_trace = list(opti.get("safety_trace") or [])
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_QUANT_SAFETY,
        agent_or_component="quant_safety_gate",
        implementation="rule_based",
        input_payload={
            "max_safety_retries": 3,
            "feedback_loops": len(safety_trace),
        },
        output_payload={
            "safety_ok": bool(opti.get("safety_ok", True)),
            "safety_trace": safety_trace,
        },
        messages=[
            m
            for m in (opti.get("messages") or [])
            if "safety" in str(m).lower()
        ]
        or ["Quant safety gate passed."],
    )
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_QUANT,
        agent_or_component="quant_agent_optimizer",
        implementation="black_litterman_cvxpy",
        input_payload=quant_input,
        output_payload={
            "weights": opti_weights,
            "positions": list(opti.get("positions") or []),
            "expected_returns": opti.get("expected_returns"),
            "iterations": opti.get("iterations"),
            "messages": list(opti.get("messages") or []),
            "safety_ok": bool(opti.get("safety_ok", True)),
        },
        messages=list(opti.get("messages") or []),
    )
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_COMPLIANCE,
        agent_or_component="compliance_resolve_loop",
        implementation="deterministic",
        input_payload={
            "trading_rules": quant_input["trading_rules"],
            "iterations": opti.get("iterations"),
        },
        output_payload={
            "violations_resolved": opti.get("violations_resolved"),
            "weights": opti_weights,
            "messages": list(opti.get("messages") or []),
        },
        messages=[
            m
            for m in (opti.get("messages") or [])
            if "compliance" in str(m).lower() or "constraint" in str(m).lower()
        ]
        or ["Compliance loop finished."],
    )

    opti_metrics = compute_client_metrics_from_weights(history.returns, opti_weights)
    delta = metrics_delta(base_metrics, opti_metrics)
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_RISK_OPTI,
        agent_or_component="risk_engine",
        implementation="deterministic",
        input_payload={"weights": opti_weights},
        output_payload={
            "opti_client_metrics": opti_metrics.model_dump(mode="json"),
            "metrics_delta": delta,
        },
    )

    # Past Analogue — opt-in, fail-open (easy rollback: flag off → no Tavily / no tilts).
    past_analogue_payload: dict[str, Any] | None = None
    pa_positions_override: list[dict[str, Any]] | None = None
    if past_analogue_enabled(feature_flags, scenario_cfg):
        try:
            knobs = past_analogue_knobs(scenario_cfg)
            apply_pa = past_analogue_apply_enabled(feature_flags, scenario_cfg)
            pa_artifact = run_past_analogue_pipeline(
                params=scenario_params,
                returns=history.returns,
                base_weights=base_weights,
                applied_weights=opti_weights,
                strategy_view=strategy_view,
                base_positions=base_positions,
                trading_rules=list(trading_rules),
                portfolio_value=portfolio_value or None,
                prices=prices,
                ticker_currency=ticker_ccy,
                hkd_per_usd=hkd_per_usd,
                client_alias=client_alias,
                risk_aversion=float(
                    scenario_params.risk_aversion
                    if scenario_params.risk_aversion is not None
                    else DEFAULT_RISK_AVERSION
                ),
                freeze_list=list(scenario_params.freeze_list or []),
                weight_overrides=dict(scenario_params.weight_overrides or {}),
                knobs=knobs,
                apply=apply_pa,
            )
            past_analogue_payload = pa_artifact.model_dump(mode="json")
            messages.extend(list(pa_artifact.messages or []))
            # Applied path: weights already ran propose_opti_folio with trading_rules.
            if apply_pa and pa_artifact.applied and pa_artifact.weights_shadow:
                opti_weights = {
                    _normalize_ticker(str(k)): float(v)
                    for k, v in pa_artifact.weights_shadow.items()
                }
                if pa_artifact.positions_shadow:
                    pa_positions_override = list(pa_artifact.positions_shadow)
                opti_metrics = compute_client_metrics_from_weights(
                    history.returns, opti_weights
                )
                delta = metrics_delta(base_metrics, opti_metrics)
                messages.append(
                    "past_analogue: applied blended strategist+analogue weights to "
                    "live opti (trading/compliance enforced in both opti legs)"
                )
            write_scenario_step_audit(
                **audit_kw,
                step=STEP_PAST_ANALOGUE,
                agent_or_component="past_analogue_agent",
                implementation="web_search_llm_extract_apply",
                input_payload={
                    "knobs": knobs,
                    "apply_requested": apply_pa,
                    "user_prompt": (scenario_params.user_prompt or "")[:500],
                },
                output_payload=past_analogue_payload,
                messages=list(pa_artifact.messages or []),
            )
        except Exception as pa_exc:  # noqa: BLE001
            logger.warning(
                "past_analogue_hook_failed",
                scenario_run_id=str(scenario_run_id),
                error=str(pa_exc),
            )
            messages.append(f"past_analogue fail-open: {pa_exc}")
            past_analogue_payload = {
                "enabled": True,
                "applied": False,
                "shadow": True,
                "messages": [f"past_analogue fail-open: {pa_exc}"],
                "chat_summary": "weight input from historical events = 0.000",
            }
            write_scenario_step_audit(
                **audit_kw,
                step=STEP_PAST_ANALOGUE,
                agent_or_component="past_analogue_agent",
                implementation="fail_open",
                input_payload={},
                output_payload=past_analogue_payload,
                messages=[str(pa_exc)],
                ok=False,
            )

    opti_position_dicts: list[dict[str, Any]] = (
        list(pa_positions_override)
        if pa_positions_override is not None
        else list(opti.get("positions") or [])
    )
    opti_positions = [OptiPosition.model_validate(p) for p in opti_position_dicts]

    staged = _build_staged_trades(
        scenario_run_id=scenario_run_id,
        base_qty=base_qty,
        base_weights=base_weights,
        opti_positions=opti_position_dicts,
        asset_classes=base_ac,
        client_alias=client_alias,
    )
    _persist_staged_trades(
        scenario_run_id=scenario_run_id,
        advisor_id=advisor_id,
        portfolio_id=portfolio_id,
        trades=staged,
        client_alias=client_alias,
    )

    talking = build_talking_points(
        metrics_delta=delta,
        base_metrics=base_metrics,
        staged_trades=staged,
        strategy_view=strategy_view,
        opti_folio=opti_positions,
        max_market_bullets=3,
    )
    news_footnotes = extract_news_footnotes(
        list(market_research.get("articles") or []),
        limit=5,
    )

    performance_payload: dict[str, Any] | None = None
    try:
        price_panel = history.prices_panel
        if price_panel is None or price_panel.empty:
            price_panel = history.prices
        perf = build_base_vs_opti_performance(
            price_panel,
            base_weights=base_weights,
            opti_weights=opti_weights,
            lookback_window=DEFAULT_CHART_LOOKBACK_WINDOW,
            target_lookback_days=DEFAULT_CHART_LOOKBACK_DAYS,
        )
        performance_payload = perf.model_dump(mode="json")
        if not perf.validation_ok:
            messages.append(
                "Performance series validation warnings: "
                + "; ".join(perf.validation_messages)
            )
        else:
            messages.append(
                f"Performance series OK ({perf.point_count_full} points, "
                f"lookback={perf.lookback_window or 'n/a'})."
            )
    except PerformanceBuildError as exc:
        messages.append(f"Performance series unavailable: {exc}")
        logger.warning(
            "scenario_performance_build_failed",
            scenario_run_id=str(scenario_run_id),
            error=str(exc),
        )

    write_scenario_step_audit(
        **audit_kw,
        step=STEP_CONTENT,
        agent_or_component="content_agent",
        implementation="deterministic_perf_and_trade_gated_color",
        input_payload={
            "metrics_delta": delta,
            "staged_trade_tickers": [t.ticker for t in staged],
            "strategy_view_source": strategy_view.source,
            "news_footnote_count": len(news_footnotes),
        },
        output_payload={
            "talking_points": [t.model_dump(mode="json") for t in talking],
            "news_footnotes": news_footnotes,
            "performance_series": performance_payload,
        },
    )

    strategy_view_json = strategy_view.model_dump(mode="json")
    strategy_view_json["notes"] = list(strategy_view.notes) + [
        f"metrics_delta={json.dumps(delta)}"
    ]
    # Persist for PDF footnotes (jsonb; ignored by StrategyView validation).
    strategy_view_json["news_footnotes"] = news_footnotes
    if performance_payload is not None:
        # Backup if performance_series column not yet migrated.
        strategy_view_json["performance_series"] = performance_payload
    # Nested jsonb — no new DB column required (easy rollback).
    if past_analogue_payload is not None:
        strategy_view_json["past_analogue"] = past_analogue_payload

    artifact: dict[str, Any] = {
        "status": "COMPLETED",
        "strategy_view": strategy_view_json,
        "risk_metrics_base": base_metrics.model_dump(mode="json"),
        "risk_metrics_opti": opti_metrics.model_dump(mode="json"),
        "opti_folio": [p.model_dump(mode="json") for p in opti_positions],
        "talking_points": [t.model_dump(mode="json") for t in talking],
        "performance_series": performance_payload,
        "error_code": None,
        "error_message": None,
        "updated_at": _now_iso(),
    }

    try:
        _client().table("scenario_runs").update(artifact).eq(
            "id", str(scenario_run_id)
        ).execute()
    except Exception as exc:  # noqa: BLE001
        # Soft fallback if performance_series column not migrated yet.
        err = str(exc).lower()
        if "performance_series" in err and "performance_series" in artifact:
            artifact.pop("performance_series", None)
            try:
                _client().table("scenario_runs").update(artifact).eq(
                    "id", str(scenario_run_id)
                ).execute()
                messages.append(
                    "performance_series column missing — apply "
                    "migration_performance_series.sql; chart data kept in-run only."
                )
            except Exception as exc2:  # noqa: BLE001
                _guard_db(exc2)
                raise
        else:
            _guard_db(exc)
            raise

    write_scenario_step_audit(
        **audit_kw,
        step=STEP_RUN_COMPLETE,
        agent_or_component="scenario_orchestrator",
        implementation="application",
        input_payload={},
        output_payload={
            "status": "COMPLETED",
            "opti_folio": [p.model_dump(mode="json") for p in opti_positions],
            "staged_trades": [t.model_dump(mode="json") for t in staged],
            "metrics_delta": delta,
            "performance_series": performance_payload,
            "past_analogue": past_analogue_payload,
        },
        messages=messages,
    )

    logger.info(
        "scenario_run_completed",
        scenario_run_id=str(scenario_run_id),
        staged_trades=len(staged),
        opti_positions=len(opti_positions),
    )
    past_analogue_model = None
    if past_analogue_payload is not None:
        try:
            past_analogue_model = PastAnalogueArtifact.model_validate(
                past_analogue_payload
            )
        except Exception:  # noqa: BLE001
            past_analogue_model = None
    return ScenarioRunResponse(
        scenario_run_id=scenario_run_id,
        portfolio_id=portfolio_id,
        status="COMPLETED",
        phase=phase,  # type: ignore[arg-type]
        scenario_params=scenario_params,
        strategy_view=strategy_view,
        base_client_metrics=base_metrics,
        opti_folio=opti_positions,
        opti_client_metrics=opti_metrics,
        metrics_delta=delta,
        staged_trades=staged,
        talking_points=talking,
        performance_series=performance_payload,
        past_analogue=past_analogue_model,
        messages=messages,
    )


async def execute_scenario_run(
    *,
    scenario_run_id: UUID,
    advisor_id: UUID,
    limits: TechnicalLimits | None = None,
) -> ScenarioRunResponse:
    """
    Phase-1 pipeline: history → stub strategy → risk → optimize/compliance →
    staged_trades → talking points → persist COMPLETED|FAILED.
    """
    row = _load_scenario_run(scenario_run_id, advisor_id=advisor_id)
    portfolio_id = UUID(str(row["portfolio_id"]))
    phase = str(row.get("phase") or "phase1")
    if phase not in {"phase1", "phase2"}:
        phase = "phase1"

    try:
        scenario_params = _params_from_row(row.get("scenario_params"))
    except ScenarioServiceError as exc:
        mark_run_failed(
            scenario_run_id,
            error_code=exc.error_code,
            error_message=str(exc),
            portfolio_id=portfolio_id,
            advisor_id=advisor_id,
        )
        fallback = ScenarioParams(
            user_prompt=str(row.get("user_prompt") or ""),
            scenario=ScenarioBlock(
                scenario_name="invalid",
                scenario_type="invalid",
            ),
        )
        return ScenarioRunResponse(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            status="FAILED",
            phase=phase,  # type: ignore[arg-type]
            scenario_params=fallback,
            error_code=exc.error_code,
            error_message=str(exc),
        )

    # Re-validate BASE before heavy work
    try:
        portfolio = _load_portfolio_for_scenario(portfolio_id)
    except ScenarioServiceError as exc:
        mark_run_failed(
            scenario_run_id,
            error_code=exc.error_code,
            error_message=str(exc),
            portfolio_id=portfolio_id,
            advisor_id=advisor_id,
        )
        return ScenarioRunResponse(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            status="FAILED",
            phase=phase,  # type: ignore[arg-type]
            scenario_params=scenario_params,
            error_code=exc.error_code,
            error_message=str(exc),
        )

    client_alias = _load_client_alias(
        str(portfolio["client_id"]) if portfolio.get("client_id") else None
    )

    try:
        _client().table("scenario_runs").update(
            {
                "status": "RUNNING",
                "error_code": None,
                "error_message": None,
                "updated_at": _now_iso(),
            }
        ).eq("id", str(scenario_run_id)).execute()
    except Exception as exc:  # noqa: BLE001
        _guard_db(exc)
        raise

    try:
        trading_rules = await load_active_trading_rules()
        # Prefer live configs.feature_flags so ops can flip research without re-parse.
        cfg_flags = _load_config_row("scenario_wf02").get("feature_flags") or {}
        if not isinstance(cfg_flags, dict):
            cfg_flags = {}
        run_flags = row.get("feature_flags") or {}
        if not isinstance(run_flags, dict):
            run_flags = {}
        feature_flags = {**run_flags, **cfg_flags}

        approved = approved_crypto_bases_from_rules(trading_rules)
        base_positions = _load_base_positions(portfolio_id)
        universe = build_scenario_universe(
            base_positions, approved_crypto=[str(c) for c in approved]
        )
        universe_tickers = [t for t, _ac in universe]
        params_dump = scenario_params.model_dump(mode="json")

        enable_house = bool(feature_flags.get("house_research_rag", True))
        enable_market = bool(feature_flags.get("market_research_web", True))

        if enable_house:
            house_research = await run_house_research(
                advisor_id=advisor_id,
                scenario_params=params_dump,
                universe_tickers=universe_tickers,
            )
        else:
            house_research = {
                "message_type": "in_house_research_view",
                "implementation": "disabled",
                "asset_views": [],
                "hit_count": 0,
                "messages": ["House research disabled by feature_flags."],
            }

        if enable_market:
            house_then_market = await asyncio.to_thread(
                run_market_research,
                scenario_params=params_dump,
                universe_tickers=universe_tickers,
                limits=limits,
            )
            market_research = house_then_market
        else:
            market_research = {
                "message_type": "external_research_view",
                "implementation": "disabled",
                "asset_views": [],
                "article_count": 0,
                "messages": ["Market research disabled by feature_flags."],
            }

        # Yahoo history + cvxpy are blocking — run off the event loop.
        return await asyncio.to_thread(
            _execute_phase1_sync,
            scenario_run_id=scenario_run_id,
            advisor_id=advisor_id,
            portfolio_id=portfolio_id,
            phase=phase,
            scenario_params=scenario_params,
            client_alias=client_alias,
            trading_rules=trading_rules,
            limits=limits,
            house_research=house_research,
            market_research=market_research,
            feature_flags=feature_flags,
        )

    except ScenarioServiceError as exc:
        mark_run_failed(
            scenario_run_id,
            error_code=exc.error_code,
            error_message=str(exc),
            portfolio_id=portfolio_id,
            advisor_id=advisor_id,
        )
        if exc.http_status >= 500 and exc.error_code == WBP_SCENARIO_SERVER:
            raise
        return ScenarioRunResponse(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            status="FAILED",
            phase=phase,  # type: ignore[arg-type]
            scenario_params=scenario_params,
            error_code=exc.error_code,
            error_message=str(exc),
        )
    except ComplianceUnresolvedError as exc:
        code = getattr(exc, "error_code", WBP_SCENARIO_COMPLIANCE) or WBP_SCENARIO_COMPLIANCE
        mark_run_failed(
            scenario_run_id,
            error_code=code,
            error_message=str(exc),
            portfolio_id=portfolio_id,
            advisor_id=advisor_id,
        )
        return ScenarioRunResponse(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            status="FAILED",
            phase=phase,  # type: ignore[arg-type]
            scenario_params=scenario_params,
            error_code=code,
            error_message=str(exc),
        )
    except ScenarioOptimizerError as exc:
        code = getattr(exc, "error_code", WBP_SCENARIO_RUN) or WBP_SCENARIO_RUN
        mark_run_failed(
            scenario_run_id,
            error_code=code,
            error_message=str(exc),
            portfolio_id=portfolio_id,
            advisor_id=advisor_id,
        )
        return ScenarioRunResponse(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            status="FAILED",
            phase=phase,  # type: ignore[arg-type]
            scenario_params=scenario_params,
            error_code=code,
            error_message=str(exc),
        )
    except ScenarioHistoryError as exc:
        mark_run_failed(
            scenario_run_id,
            error_code=WBP_SCENARIO_RUN,
            error_message=str(exc),
            portfolio_id=portfolio_id,
            advisor_id=advisor_id,
        )
        return ScenarioRunResponse(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            status="FAILED",
            phase=phase,  # type: ignore[arg-type]
            scenario_params=scenario_params,
            error_code=WBP_SCENARIO_RUN,
            error_message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        if _is_missing_relation(exc):
            mark_run_failed(
                scenario_run_id,
                error_code=WBP_SCENARIO_SERVER,
                error_message=_MIGRATION_HINT,
                portfolio_id=portfolio_id,
                advisor_id=advisor_id,
            )
            raise ScenarioServiceError(
                _MIGRATION_HINT,
                error_code=WBP_SCENARIO_SERVER,
                http_status=503,
            ) from exc
        logger.exception("scenario_run_unexpected", error=str(exc))
        mark_run_failed(
            scenario_run_id,
            error_code=WBP_SCENARIO_RUN,
            error_message=str(exc),
            portfolio_id=portfolio_id,
            advisor_id=advisor_id,
        )
        return ScenarioRunResponse(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            status="FAILED",
            phase=phase,  # type: ignore[arg-type]
            scenario_params=scenario_params,
            error_code=WBP_SCENARIO_RUN,
            error_message=str(exc),
        )


async def build_pdf_bytes(
    *,
    scenario_run_id: UUID,
    advisor_id: UUID,
) -> tuple[bytes, str, str | None]:
    """
    Render PDF from persisted run artifacts.

    Returns (pdf_bytes, filename, client_alias).
    """
    row = _load_scenario_run(scenario_run_id, advisor_id=advisor_id)
    status = str(row.get("status") or "")
    if status != "COMPLETED":
        raise ScenarioServiceError(
            f"PDF requires COMPLETED scenario_run (current status={status or 'unknown'})",
            error_code=WBP_SCENARIO_PDF,
            http_status=400,
        )

    try:
        scenario_params = _params_from_row(row.get("scenario_params"))
    except Exception as exc:  # noqa: BLE001
        raise ScenarioServiceError(
            f"Invalid scenario_params for PDF: {exc}",
            error_code=WBP_SCENARIO_PDF,
            http_status=400,
        ) from exc

    opti_raw = row.get("opti_folio") or []
    if not isinstance(opti_raw, list):
        opti_raw = []
    opti_metrics_raw = row.get("risk_metrics_opti") or {}
    base_metrics_raw = row.get("risk_metrics_base")
    talking_raw = row.get("talking_points") or []
    bullets: list[str] = []
    if isinstance(talking_raw, list):
        for item in talking_raw:
            if isinstance(item, dict) and item.get("bullet"):
                bullets.append(str(item["bullet"]))
            elif isinstance(item, str):
                bullets.append(item)

    sv_raw = row.get("strategy_view") or {}
    news_raw: list[Any] = []
    perf_raw: Any = row.get("performance_series")
    if isinstance(sv_raw, dict):
        news_raw = list(sv_raw.get("news_footnotes") or [])
        if not perf_raw:
            perf_raw = sv_raw.get("performance_series")
    footnote_lines = format_news_footnote_lines(news_raw)

    client_alias = _load_client_alias(
        str(row["client_id"]) if row.get("client_id") else None
    )
    scenario_name = scenario_params.scenario.scenario_name

    try:
        pdf = await asyncio.to_thread(
            build_scenario_pdf,
            scenario_name=scenario_name,
            opti_folio=opti_raw,
            opti_metrics=opti_metrics_raw
            if isinstance(opti_metrics_raw, dict)
            else ClientPortfolioMetrics(),
            base_metrics=base_metrics_raw if isinstance(base_metrics_raw, dict) else None,
            talking_points=bullets,
            client_alias=client_alias,
            news_footnotes=footnote_lines,
            performance_series=perf_raw if isinstance(perf_raw, dict) else None,
        )
    except Exception as exc:  # noqa: BLE001
        raise ScenarioServiceError(
            f"PDF render failed: {exc}",
            error_code=WBP_SCENARIO_PDF,
            http_status=500,
        ) from exc

    safe_name = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "_"
        for ch in scenario_name.strip()
    )[:48] or "scenario"
    filename = f"{safe_name}_{scenario_run_id.hex[:8]}.pdf"
    return pdf, filename, client_alias


__all__ = [
    "PARSE_TIMEOUT_SECONDS",
    "PARAMS_TIMEOUT_SECONDS",
    "PDF_TIMEOUT_SECONDS",
    "RUN_TIMEOUT_SECONDS",
    "ScenarioServiceError",
    "WBP_SCENARIO_TIMEOUT",
    "build_pdf_bytes",
    "execute_scenario_run",
    "mark_run_failed",
    "parse_and_create_run",
    "place_all_staged_orders",
    "update_scenario_params",
]
