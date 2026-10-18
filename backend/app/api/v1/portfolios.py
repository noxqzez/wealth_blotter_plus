"""WF-01 plural portfolio import endpoints."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request, status

from backend.app.api.http_util import correlation_id, http_error, http_server_error
from backend.app.core.error_codes import (
    WBP_IMPORT_BUILD,
    WBP_IMPORT_SERVER,
    WBP_IMPORT_VALIDATE,
)
from backend.app.core.rules_engine import RulesEngine, get_rules_engine
from backend.app.core.security import AuthContext, get_current_user
from backend.app.schemas.portfolio_import import (
    BuildBaseRequest,
    BuildBaseResponse,
    ImportValidationRequest,
    ValidateImportResponse,
)
from backend.app.services.audit_events import SOURCE_PORTFOLIO_API, write_application_event
from backend.app.services.portfolio_import import build_base, validate_import
from backend.app.services.portfolio_normalize import ImportValidationError

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.post("/validate-import", response_model=ValidateImportResponse)
async def portfolios_validate_import(
    payload: ImportValidationRequest,
    request: Request,
    user: AuthContext = Depends(get_current_user),
) -> ValidateImportResponse:
    """Boundary checks, ticker/Ccy normalization, draft_folio staging."""
    cid = correlation_id(request)
    try:
        result = await validate_import(payload, advisor_id=user.advisor_id)
    except ImportValidationError as exc:
        raise http_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code=WBP_IMPORT_VALIDATE,
            message=str(exc),
            correlation_id=cid,
        ) from exc
    except Exception as exc:  # noqa: BLE001 — map to support code
        raise http_server_error(
            exc,
            error_code=WBP_IMPORT_SERVER,
            correlation_id=cid,
            log_event="portfolio_import_server_error",
        ) from exc

    try:
        await write_application_event(
            source_name=SOURCE_PORTFOLIO_API,
            event_action="portfolio_validate_import",
            event_type="portfolio",
            advisor_id=user.advisor_id,
            actor_id=user.advisor_id,
            entity_refs={
                "client_alias": result.client_alias,
                "portfolio_id": str(result.portfolio_id),
            },
            payload={
                "draft_id": str(result.draft_id),
                "positions_count": len(result.positions),
                "messages": result.messages,
                "correlation_id": cid,
            },
            normalized_params={
                "event_action": "portfolio_validate_import",
                "correlation_id": cid,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit_event_emission_failed", error=str(exc))

    return result


@router.post("/build-base", response_model=BuildBaseResponse)
async def portfolios_build_base(
    payload: BuildBaseRequest,
    request: Request,
    user: AuthContext = Depends(get_current_user),
    rules: RulesEngine = Depends(get_rules_engine),
) -> BuildBaseResponse:
    """Yahoo enrichment, drop invalid tickers, commit base_folio with PosIDs."""
    cid = correlation_id(request)
    limits = await rules.get_technical_limits()
    try:
        result = await build_base(payload, limits=limits)
    except ImportValidationError as exc:
        raise http_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code=WBP_IMPORT_BUILD,
            message=str(exc),
            correlation_id=cid,
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise http_server_error(
            exc,
            error_code=WBP_IMPORT_SERVER,
            correlation_id=cid,
            log_event="portfolio_import_server_error",
        ) from exc

    try:
        await write_application_event(
            source_name=SOURCE_PORTFOLIO_API,
            event_action="portfolio_build_base",
            event_type="portfolio",
            advisor_id=user.advisor_id,
            actor_id=user.advisor_id,
            entity_refs={
                "client_alias": result.client_alias,
                "portfolio_id": str(result.portfolio_id),
            },
            payload={
                "draft_id": str(result.draft_id),
                "positions_count": len(result.positions),
                "warnings": result.warnings,
                "pos_ids": [str(p.pos_id) for p in result.positions],
                "correlation_id": cid,
            },
            normalized_params={
                "event_action": "portfolio_build_base",
                "correlation_id": cid,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit_event_emission_failed", error=str(exc))

    return result
