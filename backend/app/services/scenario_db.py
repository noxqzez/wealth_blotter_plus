"""Shared scenario DB primitives: error type, client, loaders, config."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

from backend.app.core.error_codes import (
    WBP_SCENARIO_BASE_REQUIRED,
    WBP_SCENARIO_CLIENT,
    WBP_SCENARIO_PARAMS,
    WBP_SCENARIO_SERVER,
)
from backend.app.core.supabase import get_service_supabase
from backend.app.schemas.scenario import ScenarioParams
from backend.app.services.ticker_normalize import normalize_ticker as _normalize_ticker

logger = structlog.get_logger(__name__)

_SEED_CONFIG_DIR = Path(__file__).resolve().parent.parent / "db" / "seed_configs"
_MIGRATION_HINT = (
    "scenario_runs table missing — apply backend/app/db/migration_scenario_wf02.sql "
    "in the Supabase SQL Editor, then retry."
)

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
