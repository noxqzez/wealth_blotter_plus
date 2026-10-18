"""Shared FastAPI request helpers for /api/v1 routers."""

from __future__ import annotations

import structlog
from fastapi import HTTPException, Request, status

from backend.app.core.error_codes import error_payload

logger = structlog.get_logger(__name__)


def correlation_id(request: Request) -> str:
    """Read correlation id attached by middleware (empty string if absent)."""
    return str(getattr(request.state, "correlation_id", "") or "")


def http_error(
    *,
    status_code: int,
    error_code: str,
    message: str,
    correlation_id: str = "",
    support: bool = False,
) -> HTTPException:
    """Build a structured API error (client or server) with standard payload."""
    return HTTPException(
        status_code=status_code,
        detail=error_payload(
            error_code=error_code,
            message=message,
            correlation_id=correlation_id or None,
            support=support,
        ),
    )


def http_server_error(
    exc: Exception,
    *,
    error_code: str,
    correlation_id: str,
    log_event: str = "api_server_error",
) -> HTTPException:
    """Map unexpected exceptions to a support-facing 500 payload."""
    logger.exception(
        log_event,
        error=str(exc),
        error_code=error_code,
        correlation_id=correlation_id,
    )
    return http_error(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        error_code=error_code,
        message=str(exc),
        correlation_id=correlation_id,
        support=True,
    )
