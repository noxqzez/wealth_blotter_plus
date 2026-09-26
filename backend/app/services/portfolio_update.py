"""Portfolio UPDATE PORTFOLIO orchestration (MVP)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import structlog

from backend.app.core.supabase import get_service_supabase
from backend.app.schemas.payload_models import (
    PortfolioPasteRow,
    PortfolioPositionRow,
    PortfolioUpdateRequest,
    PortfolioUpdateResponse,
)
from backend.app.schemas.rules import TechnicalLimits
from backend.app.services.market_data import fetch_market_quotes
from backend.app.services.market_fx import (
    currency_for_ticker,
    fetch_hkd_per_usd,
    notional_usd,
)

logger = structlog.get_logger(__name__)


async def update_portfolio_from_rows(
    payload: PortfolioUpdateRequest,
    *,
    limits: TechnicalLimits | None = None,
) -> PortfolioUpdateResponse:
    """
    Group paste rows by client_alias, upsert client + portfolio,
    replace positions with Yahoo-priced notionals and weights.
    """
    grouped: dict[str, list[PortfolioPasteRow]] = defaultdict(list)
    for row in payload.rows:
        grouped[row.client_alias.strip()].append(row)

    all_tickers = [r.ticker.strip().upper() for r in payload.rows]
    quotes = fetch_market_quotes(all_tickers, limits=limits)
    hkd_per_usd = fetch_hkd_per_usd(limits=limits)

    client = get_service_supabase()
    blotter: list[PortfolioPositionRow] = []
    clients_touched: list[str] = []

    for alias, rows in grouped.items():
        clients_touched.append(alias)
        client_id = _upsert_client(client, alias)
        portfolio_id = _upsert_portfolio(client, client_id)

        priced_rows: list[dict[str, Any]] = []
        for row in rows:
            ticker = row.ticker.strip().upper()
            quote = quotes.get(ticker)
            qty = float(row.quantity)
            local_spot = float(quote.spot_price) if quote else 0.0
            ccy = currency_for_ticker(ticker)
            # Last price = local (HKD for *.HK); USD notional only via FX.
            usd_notional = round(
                notional_usd(
                    qty, local_spot, ccy, hkd_per_usd=hkd_per_usd, limits=limits
                ),
                2,
            )
            short_name = quote.short_name if quote else ticker
            priced_rows.append(
                {
                    "ticker": ticker,
                    "short_name": short_name,
                    "asset_class": row.asset_class.strip().upper(),
                    "quantity": qty,
                    "spot_price": round(local_spot, 6),
                    "notional_value": usd_notional,
                    "currency": ccy,
                }
            )

        # Aggregate duplicate tickers within the same client paste
        aggregated: dict[str, dict[str, Any]] = {}
        for item in priced_rows:
            key = item["ticker"]
            if key not in aggregated:
                aggregated[key] = item
            else:
                aggregated[key]["quantity"] += item["quantity"]
                aggregated[key]["notional_value"] = round(
                    notional_usd(
                        aggregated[key]["quantity"],
                        aggregated[key]["spot_price"],
                        aggregated[key]["currency"],
                        hkd_per_usd=hkd_per_usd,
                        limits=limits,
                    ),
                    2,
                )

        total_notional = sum(v["notional_value"] for v in aggregated.values())
        now = datetime.now(timezone.utc).isoformat()

        # Upsert by (portfolio_id, ticker) so PosID (id) stays stable across refreshes.
        existing = (
            client.table("portfolio_positions")
            .select("id,ticker")
            .eq("portfolio_id", str(portfolio_id))
            .execute()
        )
        existing_by_ticker = {
            str(row["ticker"]).upper(): str(row["id"])
            for row in (existing.data or [])
        }
        keep_tickers = set(aggregated.keys())

        upserts: list[dict[str, Any]] = []
        for item in aggregated.values():
            weight = (
                round(item["notional_value"] / total_notional, 6)
                if total_notional > 0
                else 0.0
            )
            prior_id = existing_by_ticker.get(item["ticker"])
            row: dict[str, Any] = {
                "id": prior_id or str(uuid4()),
                "portfolio_id": str(portfolio_id),
                "ticker": item["ticker"],
                "short_name": item["short_name"],
                "asset_class": item["asset_class"],
                "quantity": item["quantity"],
                "currency": item.get("currency") or currency_for_ticker(item["ticker"]),
                "spot_price": item["spot_price"],
                "notional_value": item["notional_value"],
                "current_weight_pct": weight,
                "valuation_timestamp": now,
            }
            upserts.append(row)

        if upserts:
            client.table("portfolio_positions").upsert(
                upserts,
                on_conflict="portfolio_id,ticker",
            ).execute()

        stale = [t for t in existing_by_ticker if t not in keep_tickers]
        for ticker in stale:
            client.table("portfolio_positions").delete().eq(
                "portfolio_id", str(portfolio_id)
            ).eq("ticker", ticker).execute()

        client.table("portfolios").update({"updated_at": now}).eq(
            "id", str(portfolio_id)
        ).execute()

        refreshed = (
            client.table("portfolio_positions")
            .select("*")
            .eq("portfolio_id", str(portfolio_id))
            .execute()
        )
        by_ticker = {
            str(row["ticker"]).upper(): row for row in (refreshed.data or [])
        }

        for item in aggregated.values():
            stored = by_ticker.get(item["ticker"], {})
            pos_id_raw = stored.get("id") or existing_by_ticker.get(item["ticker"])
            if not pos_id_raw:
                logger.warning(
                    "pos_id_missing_after_upsert",
                    portfolio_id=str(portfolio_id),
                    ticker=item["ticker"],
                )
                continue
            blotter.append(
                PortfolioPositionRow(
                    client_alias=alias,
                    portfolio_id=portfolio_id,
                    pos_id=UUID(str(pos_id_raw)),
                    ticker=item["ticker"],
                    asset_class=item["asset_class"],
                    quantity=float(item["quantity"]),
                    short_name=str(item["short_name"]),
                    spot_price=float(item["spot_price"]),
                    notional_value=float(item["notional_value"]),
                    current_weight_pct=float(
                        stored.get("current_weight_pct")
                        or (
                            round(item["notional_value"] / total_notional, 6)
                            if total_notional > 0
                            else 0.0
                        )
                    ),
                    valuation_timestamp=str(
                        stored.get("valuation_timestamp") or now
                    ),
                )
            )

    logger.info(
        "portfolio_update_complete",
        clients=len(clients_touched),
        positions=len(blotter),
    )
    return PortfolioUpdateResponse(
        clients_updated=clients_touched,
        positions=blotter,
    )


def _upsert_client(client: Any, alias: str) -> UUID:
    existing = (
        client.table("clients").select("id").eq("client_alias", alias).limit(1).execute()
    )
    if existing.data:
        return UUID(existing.data[0]["id"])
    created = (
        client.table("clients")
        .insert({"client_alias": alias})
        .execute()
    )
    return UUID(created.data[0]["id"])


def _upsert_portfolio(client: Any, client_id: UUID) -> UUID:
    existing = (
        client.table("portfolios")
        .select("id")
        .eq("client_id", str(client_id))
        .limit(1)
        .execute()
    )
    if existing.data:
        return UUID(existing.data[0]["id"])
    created = (
        client.table("portfolios")
        .insert({"client_id": str(client_id)})
        .execute()
    )
    return UUID(created.data[0]["id"])
