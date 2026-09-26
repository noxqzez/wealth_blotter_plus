"""MVP portfolio update endpoints (deprecated — use /api/v1/portfolios/*)."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from backend.app.core.rules_engine import RulesEngine, get_rules_engine
from backend.app.core.security import AuthContext, get_current_user
from backend.app.schemas.payload_models import (
    PortfolioPositionRow,
    PortfolioUpdateRequest,
    PortfolioUpdateResponse,
)
from backend.app.schemas.portfolio_import import (
    BuildBaseRequest,
    ImportValidationRequest,
    RawImportRow,
)
from backend.app.services.audit_events import SOURCE_PORTFOLIO_API, write_application_event
from backend.app.services.portfolio_import import build_base, validate_import
from backend.app.services.portfolio_normalize import ImportValidationError

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.post(
    "/update",
    response_model=PortfolioUpdateResponse,
    deprecated=True,
    summary="Deprecated: use POST /api/v1/portfolios/validate-import then /build-base",
)
async def update_portfolio(
    payload: PortfolioUpdateRequest,
    user: AuthContext = Depends(get_current_user),
    rules: RulesEngine = Depends(get_rules_engine),
) -> PortfolioUpdateResponse:
    """
    Backward-compatible wrapper: validate-import → build-base.
    Prefer plural WF-01 routes from Excel / new clients.
    """
    limits = await rules.get_technical_limits()
    if len(payload.rows) > limits.max_batch_ingest_rows:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Row count exceeds max_batch_ingest_rows ({limits.max_batch_ingest_rows})",
        )

    raw = ImportValidationRequest(
        rows=[
            RawImportRow(
                client_alias=r.client_alias,
                ticker=r.ticker,
                quantity=r.quantity,
                asset_class=r.asset_class,
                currency=r.currency,
            )
            for r in payload.rows
        ]
    )
    try:
        draft = await validate_import(raw)
        built = await build_base(
            BuildBaseRequest(portfolio_id=draft.portfolio_id, draft_id=draft.draft_id),
            limits=limits,
        )
    except ImportValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    positions = [
        PortfolioPositionRow(
            client_alias=p.client_alias,
            portfolio_id=p.portfolio_id,
            pos_id=p.pos_id,
            ticker=p.ticker,
            asset_class=p.asset_class,
            quantity=p.quantity,
            short_name=p.short_name,
            spot_price=p.spot_price,
            notional_value=p.market_value,
            current_weight_pct=p.current_weight_pct,
            valuation_timestamp=(
                p.price_timestamp.isoformat() if p.price_timestamp else ""
            ),
            currency=p.currency,
            price_status=p.status,
        )
        for p in built.positions
    ]
    result = PortfolioUpdateResponse(
        clients_updated=[built.client_alias],
        positions=positions,
    )

    try:
        await write_application_event(
            source_name=SOURCE_PORTFOLIO_API,
            event_action="portfolio_update",
            event_type="portfolio",
            advisor_id=user.advisor_id,
            actor_id=user.advisor_id,
            entity_refs={
                "client_alias": built.client_alias,
                "portfolio_id": str(built.portfolio_id),
            },
            payload={
                "deprecated_wrapper": True,
                "clients_updated": result.clients_updated,
                "positions_count": len(result.positions),
                "warnings": built.warnings,
                "pos_ids": [str(p.pos_id) for p in result.positions],
            },
            normalized_params={"event_action": "portfolio_update"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit_event_emission_failed", error=str(exc))

    return result
