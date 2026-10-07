"""Read-only identifier recall for Excel Copilot chat.

Advisors may reference portfolio / scenario / order / position IDs to audit
prior results. Chat never mutates session IDs or persisted rows from these
lookups.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

import structlog
from pydantic import BaseModel, Field

from backend.app.core.supabase import get_service_supabase

logger = structlog.get_logger(__name__)

IdKind = Literal["portfolio_id", "scenario_run_id", "order_id", "position_id"]


class IdReference(BaseModel):
    kind: IdKind
    value: str = Field(..., min_length=1, max_length=128)


def _client():
    return get_service_supabase()


def _safe_uuid(raw: str) -> UUID | None:
    try:
        return UUID(str(raw).strip())
    except (ValueError, TypeError, AttributeError):
        return None


def _recall_portfolio(value: str) -> str:
    uid = _safe_uuid(value)
    if uid is None:
        return f"**portfolioID** `{value}` — invalid UUID."
    try:
        result = (
            _client()
            .table("portfolios")
            .select("id, folio_stage, currency, cash_balance, updated_at")
            .eq("id", str(uid))
            .limit(1)
            .execute()
        )
        pos = (
            _client()
            .table("portfolio_positions")
            .select("id")
            .eq("portfolio_id", str(uid))
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot_id_recall_portfolio_failed", error=str(exc))
        return f"**portfolioID** `{uid}` — lookup failed (read-only)."
    if not result.data:
        return f"**portfolioID** `{uid}` — not found."
    row = result.data[0]
    n_pos = len(pos.data or [])
    return (
        f"**portfolioID** `{uid}` (read-only)\n"
        f"- folio_stage: `{row.get('folio_stage')}`\n"
        f"- currency: `{row.get('currency')}`\n"
        f"- cash_balance: `{row.get('cash_balance')}`\n"
        f"- positions: `{n_pos}`\n"
        f"- updated_at: `{row.get('updated_at')}`"
    )


def _recall_scenario_run(*, value: str, advisor_id: UUID) -> str:
    uid = _safe_uuid(value)
    if uid is None:
        return f"**scenarioID** `{value}` — invalid UUID."
    try:
        result = (
            _client()
            .table("scenario_runs")
            .select(
                "id, status, phase, portfolio_id, user_prompt, created_at, "
                "updated_at, orders_hitl, error_code"
            )
            .eq("id", str(uid))
            .eq("advisor_id", str(advisor_id))
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot_id_recall_scenario_failed", error=str(exc))
        return f"**scenarioID** `{uid}` — lookup failed (read-only)."
    if not result.data:
        return f"**scenarioID** `{uid}` — not found for your advisor."
    row = result.data[0]
    prompt = str(row.get("user_prompt") or "")
    if len(prompt) > 160:
        prompt = prompt[:157] + "..."
    return (
        f"**scenarioID** `{uid}` (read-only)\n"
        f"- status: `{row.get('status')}`\n"
        f"- phase: `{row.get('phase')}`\n"
        f"- portfolioID: `{row.get('portfolio_id')}`\n"
        f"- orders_hitl: `{row.get('orders_hitl')}`\n"
        f"- error_code: `{row.get('error_code') or '—'}`\n"
        f"- prompt: {prompt or '—'}\n"
        f"- created_at: `{row.get('created_at')}`"
    )


def _recall_order(*, value: str, advisor_id: UUID) -> str:
    oid = value.strip()
    try:
        result = (
            _client()
            .table("staged_trades")
            .select(
                "order_id, scenario_run_id, portfolio_id, status, ticker, "
                "asset_class, side, quantity, exec_type"
            )
            .eq("order_id", oid)
            .eq("advisor_id", str(advisor_id))
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot_id_recall_order_failed", error=str(exc))
        return f"**orderID** `{oid}` — lookup failed (read-only)."
    if not result.data:
        return f"**orderID** `{oid}` — not found for your advisor."
    row = result.data[0]
    return (
        f"**orderID** `{oid}` (read-only)\n"
        f"- status: `{row.get('status')}`\n"
        f"- ticker: `{row.get('ticker')}` ({row.get('asset_class')})\n"
        f"- side/qty: `{row.get('side')}` / `{row.get('quantity')}`\n"
        f"- exec_type: `{row.get('exec_type')}`\n"
        f"- scenarioID: `{row.get('scenario_run_id')}`\n"
        f"- portfolioID: `{row.get('portfolio_id')}`"
    )


def _recall_position(*, value: str, advisor_id: UUID) -> str:
    uid = _safe_uuid(value)
    if uid is None:
        return f"**positionID** `{value}` — invalid UUID."
    try:
        result = (
            _client()
            .table("portfolio_positions")
            .select(
                "id, portfolio_id, ticker, asset_class, quantity, currency, "
                "current_weight_pct, price_status"
            )
            .eq("id", str(uid))
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot_id_recall_position_failed", error=str(exc))
        return f"**positionID** `{uid}` — lookup failed (read-only)."
    if not result.data:
        return f"**positionID** `{uid}` — not found."
    row = result.data[0]
    portfolio_id = row.get("portfolio_id")
    # Soft ownership: advisor must own at least one scenario_run on that portfolio.
    owned = False
    if portfolio_id:
        try:
            check = (
                _client()
                .table("scenario_runs")
                .select("id")
                .eq("portfolio_id", str(portfolio_id))
                .eq("advisor_id", str(advisor_id))
                .limit(1)
                .execute()
            )
            owned = bool(check.data)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "copilot_id_recall_position_acl_failed", error=str(exc)
            )
            owned = False
    if not owned:
        return f"**positionID** `{uid}` — not found for your advisor."
    return (
        f"**positionID** `{uid}` (read-only)\n"
        f"- portfolioID: `{portfolio_id}`\n"
        f"- ticker: `{row.get('ticker')}` ({row.get('asset_class')})\n"
        f"- quantity: `{row.get('quantity')}`\n"
        f"- currency: `{row.get('currency')}`\n"
        f"- weight: `{row.get('current_weight_pct')}`\n"
        f"- price_status: `{row.get('price_status')}`"
    )


def format_id_recall(
    refs: list[IdReference],
    *,
    advisor_id: UUID,
) -> str:
    """Return markdown summaries for referenced IDs (never mutates state)."""
    if not refs:
        return (
            "No identifiers found. You can recall a portfolioID, scenarioID, "
            "orderID, or positionID for audit (read-only)."
        )
    blocks: list[str] = [
        "Read-only recall (identifiers cannot be set via chat):\n"
    ]
    for ref in refs:
        if ref.kind == "portfolio_id":
            blocks.append(_recall_portfolio(ref.value))
        elif ref.kind == "scenario_run_id":
            blocks.append(
                _recall_scenario_run(value=ref.value, advisor_id=advisor_id)
            )
        elif ref.kind == "order_id":
            blocks.append(_recall_order(value=ref.value, advisor_id=advisor_id))
        elif ref.kind == "position_id":
            blocks.append(
                _recall_position(value=ref.value, advisor_id=advisor_id)
            )
        else:
            blocks.append(f"Unknown id kind `{ref.kind}`.")
    return "\n\n".join(blocks)


__all__ = ["IdKind", "IdReference", "format_id_recall"]
