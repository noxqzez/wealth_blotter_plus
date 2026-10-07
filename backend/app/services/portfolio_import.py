"""WF-01 portfolio import: validate-import → build-base."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import structlog

from backend.app.core.supabase import get_service_supabase
from backend.app.schemas.portfolio_import import (
    BuildBaseRequest,
    BuildBaseResponse,
    DraftPosition,
    EnrichedPosition,
    ImportValidationRequest,
    ValidateImportResponse,
)
from backend.app.schemas.rules import TechnicalLimits
from backend.app.services.market_data import MarketQuote, fetch_market_quotes
from backend.app.services.market_fx import fetch_hkd_per_usd, notional_usd
from backend.app.services.portfolio_normalize import (
    ImportValidationError,
    normalize_import_rows,
)
from backend.app.services.compliance_cite import load_active_trading_rules
from backend.app.services.trading_rules_policy import approved_crypto_bases_from_rules

logger = structlog.get_logger(__name__)


async def validate_import(
    payload: ImportValidationRequest,
    *,
    advisor_id: UUID | None = None,
) -> ValidateImportResponse:
    """Normalize paste rows, persist draft_folio; messages go to API + audit."""
    trading_rules = await load_active_trading_rules()
    approved = approved_crypto_bases_from_rules(trading_rules)
    try:
        result = normalize_import_rows(payload.rows, approved_crypto=approved)
    except ImportValidationError:
        raise

    draft_id = uuid4()
    client = get_service_supabase()
    client_id = _upsert_client(client, result.client_alias, advisor_id=advisor_id)
    portfolio_id = _upsert_portfolio(client, client_id)

    draft_positions = [
        DraftPosition(
            client_alias=p.client_alias,
            asset_class=p.asset_class,
            ticker=p.ticker,
            quantity=p.quantity,
            currency=p.currency,
            currency_derived=p.currency_derived,
        )
        for p in result.positions
    ]
    draft_payload = [p.model_dump() for p in draft_positions]
    now = datetime.now(timezone.utc).isoformat()

    client.table("portfolios").update(
        {
            "folio_stage": "DRAFT",
            "import_draft_id": str(draft_id),
            "draft_folio": draft_payload,
            "updated_at": now,
        }
    ).eq("id", str(portfolio_id)).execute()

    logger.info(
        "portfolio_validate_import",
        portfolio_id=str(portfolio_id),
        draft_id=str(draft_id),
        positions=len(draft_positions),
        messages=len(result.messages),
    )
    return ValidateImportResponse(
        portfolio_id=portfolio_id,
        draft_id=draft_id,
        client_alias=result.client_alias,
        positions=draft_positions,
        messages=result.messages,
    )


async def build_base(
    payload: BuildBaseRequest,
    *,
    limits: TechnicalLimits | None = None,
) -> BuildBaseResponse:
    """Enrich draft_folio via Yahoo, drop invalid tickers, commit base positions + PosIDs."""
    client = get_service_supabase()
    portfolio_id = payload.portfolio_id

    folio = (
        client.table("portfolios")
        .select("id, client_id, folio_stage, import_draft_id, draft_folio")
        .eq("id", str(portfolio_id))
        .limit(1)
        .execute()
    )
    if not folio.data:
        raise ImportValidationError("Portfolio not found")
    row = folio.data[0]
    draft_id_raw = payload.draft_id or row.get("import_draft_id")
    if not draft_id_raw:
        raise ImportValidationError("No import draft found; run validate-import first")
    draft_id = UUID(str(draft_id_raw))
    if payload.draft_id and str(row.get("import_draft_id")) != str(payload.draft_id):
        raise ImportValidationError("draft_id does not match portfolio import session")

    draft_raw = row.get("draft_folio") or []
    if not isinstance(draft_raw, list) or not draft_raw:
        raise ImportValidationError("draft_folio is empty; run validate-import first")

    draft_positions = [DraftPosition.model_validate(p) for p in draft_raw]
    client_alias = draft_positions[0].client_alias

    warnings: list[str] = []
    enriched_draft: list[dict[str, Any]] = []

    # Cash: fixed spot 1.0 — skip Yahoo
    cash_rows = [p for p in draft_positions if p.asset_class == "CASH"]
    market_rows = [p for p in draft_positions if p.asset_class != "CASH"]
    tickers = [p.ticker for p in market_rows]

    quotes: dict[str, MarketQuote] = {}
    if tickers:
        quotes = await asyncio.to_thread(fetch_market_quotes, tickers, limits=limits)

    for pos in cash_rows:
        enriched_draft.append(
            {
                "draft": pos,
                "spot": 1.0,
                "short_name": pos.ticker,
                "status": "VALID",
            }
        )

    for pos in market_rows:
        quote = quotes.get(pos.ticker)
        if quote is None or quote.listing_status == "NOT_FOUND":
            warnings.append(f"Invalid Ticker {pos.ticker}, row removed")
            continue

        if quote.listing_status == "UNAVAILABLE" or quote.spot_price <= 0:
            warnings.append(f"{pos.ticker} last price unavailable")
            enriched_draft.append(
                {
                    "draft": pos,
                    "spot": 0.0,
                    "short_name": quote.short_name if quote else pos.ticker,
                    "status": "MANUAL_PRICE_REQUIRED",
                }
            )
            continue

        enriched_draft.append(
            {
                "draft": pos,
                "spot": float(quote.spot_price),
                "short_name": quote.short_name,
                "status": "VALID",
            }
        )

    if not enriched_draft:
        raise ImportValidationError("No valid positions remain after market enrichment")

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    hkd_per_usd = fetch_hkd_per_usd()
    priced: list[dict[str, Any]] = []
    for item in enriched_draft:
        d: DraftPosition = item["draft"]
        local_spot = float(item["spot"])
        # Last price stays in local ccy (HKD for *.HK); FX only for USD notional.
        notional_usd_val = round(
            notional_usd(d.quantity, local_spot, d.currency, hkd_per_usd=hkd_per_usd),
            2,
        )
        priced.append(
            {
                "ticker": d.ticker,
                "asset_class": d.asset_class,
                "quantity": d.quantity,
                "currency": d.currency,
                "short_name": item["short_name"],
                "spot_price": round(local_spot, 6),
                "notional_value": notional_usd_val,
                "price_status": item["status"],
            }
        )

    total_usd = sum(float(p["notional_value"]) for p in priced)

    existing = (
        client.table("portfolio_positions")
        .select("id,ticker")
        .eq("portfolio_id", str(portfolio_id))
        .execute()
    )
    existing_by_ticker = {
        str(r["ticker"]).upper(): str(r["id"]) for r in (existing.data or [])
    }
    keep = {p["ticker"] for p in priced}

    upserts: list[dict[str, Any]] = []
    for item in priced:
        weight = (
            round(float(item["notional_value"]) / total_usd, 6) if total_usd > 0 else 0.0
        )
        prior = existing_by_ticker.get(item["ticker"])
        row_out: dict[str, Any] = {
            "id": prior or str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "ticker": item["ticker"],
            "asset_class": item["asset_class"],
            "quantity": item["quantity"],
            "currency": item["currency"],
            "short_name": item["short_name"],
            "spot_price": item["spot_price"],
            "notional_value": item["notional_value"],
            "current_weight_pct": weight,
            "price_status": item["price_status"],
            "valuation_timestamp": now_iso,
        }
        upserts.append(row_out)

    if upserts:
        client.table("portfolio_positions").upsert(
            upserts, on_conflict="portfolio_id,ticker"
        ).execute()

    for ticker in existing_by_ticker:
        if ticker not in keep:
            client.table("portfolio_positions").delete().eq(
                "portfolio_id", str(portfolio_id)
            ).eq("ticker", ticker).execute()

    usd_cash = next(
        (p["quantity"] for p in priced if p["asset_class"] == "CASH" and p["ticker"] == "USD"),
        0.0,
    )
    client.table("portfolios").update(
        {
            "folio_stage": "BASE",
            "draft_folio": [],
            "cash_balance": usd_cash,
            "updated_at": now_iso,
        }
    ).eq("id", str(portfolio_id)).execute()

    refreshed = (
        client.table("portfolio_positions")
        .select("*")
        .eq("portfolio_id", str(portfolio_id))
        .execute()
    )
    by_ticker = {str(r["ticker"]).upper(): r for r in (refreshed.data or [])}

    positions: list[EnrichedPosition] = []
    for item in priced:
        stored = by_ticker.get(item["ticker"], {})
        pos_id_raw = stored.get("id") or existing_by_ticker.get(item["ticker"])
        if not pos_id_raw:
            logger.warning(
                "pos_id_missing_after_build_base",
                portfolio_id=str(portfolio_id),
                ticker=item["ticker"],
            )
            continue
        ts_raw = stored.get("valuation_timestamp") or now_iso
        ts = (
            datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            if isinstance(ts_raw, str)
            else now
        )
        positions.append(
            EnrichedPosition(
                pos_id=UUID(str(pos_id_raw)),
                portfolio_id=portfolio_id,
                client_alias=client_alias,
                asset_class=item["asset_class"],
                ticker=item["ticker"],
                quantity=float(item["quantity"]),
                spot_price=float(item["spot_price"]),
                market_value=float(item["notional_value"]),
                currency=item["currency"],
                short_name=str(item["short_name"]),
                current_weight_pct=float(stored.get("current_weight_pct") or 0.0),
                price_timestamp=ts,
                status=item["price_status"],
            )
        )

    logger.info(
        "portfolio_build_base",
        portfolio_id=str(portfolio_id),
        draft_id=str(draft_id),
        positions=len(positions),
        warnings=len(warnings),
    )
    return BuildBaseResponse(
        portfolio_id=portfolio_id,
        draft_id=draft_id,
        client_alias=client_alias,
        positions=positions,
        warnings=warnings,
    )


def _upsert_client(
    client: Any,
    alias: str,
    *,
    advisor_id: UUID | None = None,
) -> UUID:
    q = client.table("clients").select("id").eq("client_alias", alias)
    if advisor_id is not None:
        q = q.eq("advisor_id", str(advisor_id))
    existing = q.limit(1).execute()
    if existing.data:
        return UUID(existing.data[0]["id"])
    row: dict[str, Any] = {"client_alias": alias}
    if advisor_id is not None:
        row["advisor_id"] = str(advisor_id)
    created = client.table("clients").insert(row).execute()
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
    created = client.table("portfolios").insert({"client_id": str(client_id)}).execute()
    return UUID(created.data[0]["id"])


def set_portfolio_import_hitl(portfolio_id: UUID, *, hitl: bool) -> None:
    """Persist import HITL flag for garbage→folio quality KPI."""
    client = get_service_supabase()
    now = datetime.now(timezone.utc).isoformat()
    client.table("portfolios").update(
        {"import_hitl": bool(hitl), "updated_at": now}
    ).eq("id", str(portfolio_id)).execute()
    logger.info(
        "portfolio_import_hitl",
        portfolio_id=str(portfolio_id),
        import_hitl=bool(hitl),
    )
