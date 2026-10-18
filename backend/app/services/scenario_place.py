"""Staged-trade persistence and dummy OMS place-all."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog

from backend.app.core.error_codes import WBP_SCENARIO_CLIENT
from backend.app.schemas.scenario import StagedTrade
from backend.app.services.scenario_db import (
    ScenarioServiceError,
    _client,
    _guard_db,
)

logger = structlog.get_logger(__name__)


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
