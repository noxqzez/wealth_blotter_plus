"""Data ingestion endpoints — enforce technical_limits at the boundary."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from backend.app.api.http_util import http_error
from backend.app.core.error_codes import WBP_IMPORT_VALIDATE
from backend.app.core.rules_engine import RulesEngine, get_rules_engine
from backend.app.core.security import AuthContext, get_current_user
from backend.app.schemas.payload_models import IngestTickersRequest, IngestTickersResponse

router = APIRouter()


@router.post("/tickers", response_model=IngestTickersResponse)
async def ingest_tickers(
    payload: IngestTickersRequest,
    user: AuthContext = Depends(get_current_user),
    rules: RulesEngine = Depends(get_rules_engine),
) -> IngestTickersResponse:
    """Validate ticker batch size against `technical_limits` (400 on violation)."""
    limits = await rules.get_technical_limits()
    max_tickers = limits.max_tickers_per_request
    if len(payload.tickers) > max_tickers:
        raise http_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code=WBP_IMPORT_VALIDATE,
            message=f"Ticker count exceeds max_tickers_per_request ({max_tickers})",
        )
    # Anonymized advisor id only — never log PII
    return IngestTickersResponse(
        accepted=len(payload.tickers),
        advisor_id=user.advisor_id,
        tickers=payload.tickers,
    )
