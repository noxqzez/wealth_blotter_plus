"""Persist EOD equity/crypto closes and scenario-used snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog

from backend.app.core.supabase import get_service_supabase
from backend.app.schemas.payload_models import MarketEodPriceBar

logger = structlog.get_logger(__name__)


def upsert_market_eod_prices(bars: list[MarketEodPriceBar]) -> list[dict[str, Any]]:
    """
    Upsert canonical EOD bars (unique on ticker, price_date, source_name).
    Crypto bars must already use equity-session price_date for alignment.
    """
    if not bars:
        return []
    client = get_service_supabase()
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "ticker": b.ticker.strip().upper(),
            "asset_class": b.asset_class.strip(),
            "price_date": b.price_date,
            "close_price": b.close_price,
            "currency": b.currency.strip().upper(),
            "source_name": b.source_name.strip(),
            "provider_symbol": b.provider_symbol,
            "fetched_at": now,
        }
        for b in bars
    ]
    result = (
        client.table("market_eod_prices")
        .upsert(rows, on_conflict="ticker,price_date,source_name")
        .execute()
    )
    data = list(result.data or [])
    logger.info("market_eod_prices_upserted", count=len(data))
    return data


def snapshot_scenario_market_prices(
    *,
    scenario_run_id: UUID,
    bars: list[MarketEodPriceBar],
    advisor_id: UUID | None = None,
    also_upsert_canonical: bool = True,
) -> list[dict[str, Any]]:
    """
    Freeze the EOD series used by a scenario/agent run for audit.
    Optionally upsert the same bars into market_eod_prices for chart reuse.
    """
    if not bars:
        return []

    eod_by_key: dict[tuple[str, str, str], str] = {}
    if also_upsert_canonical:
        upserted = upsert_market_eod_prices(bars)
        for row in upserted:
            key = (
                str(row["ticker"]).upper(),
                str(row["price_date"]),
                str(row["source_name"]),
            )
            eod_by_key[key] = str(row["id"])

    client = get_service_supabase()
    inserts: list[dict[str, Any]] = []
    for bar in bars:
        ticker = bar.ticker.strip().upper()
        key = (ticker, bar.price_date, bar.source_name.strip())
        inserts.append(
            {
                "scenario_run_id": str(scenario_run_id),
                "advisor_id": str(advisor_id) if advisor_id else None,
                "ticker": ticker,
                "asset_class": bar.asset_class.strip(),
                "price_date": bar.price_date,
                "close_price": bar.close_price,
                "currency": bar.currency.strip().upper(),
                "source_name": bar.source_name.strip(),
                "market_eod_price_id": eod_by_key.get(key),
            }
        )

    result = client.table("scenario_market_price_snapshots").insert(inserts).execute()
    data = list(result.data or [])
    logger.info(
        "scenario_market_price_snapshots_written",
        scenario_run_id=str(scenario_run_id),
        count=len(data),
    )
    return data
