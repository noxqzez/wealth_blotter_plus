"""Host-only pilot admin: approve + send welcome email."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException, status

from backend.app.config import get_settings
from backend.app.schemas.pilot_admin import (
    ApproveAndWelcomeRequest,
    ApproveAndWelcomeResponse,
)
from backend.app.services import pilot_admin as admin_svc

router = APIRouter()

WBP_ADMIN_AUTH = "WBP-ADMIN-AUTH"


def _require_admin_key(x_admin_key: str | None) -> None:
    settings = get_settings()
    expected = (settings.admin_api_key or "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": WBP_ADMIN_AUTH,
                "message": (
                    "ADMIN_API_KEY is not configured on the server. "
                    "Set it in Render / .env before using /admin/pilots."
                ),
            },
        )
    provided = (x_admin_key or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": WBP_ADMIN_AUTH,
                "message": "Invalid or missing X-Admin-Key.",
            },
        )


@router.post(
    "/approve-and-welcome",
    response_model=ApproveAndWelcomeResponse,
)
async def approve_and_welcome(
    payload: ApproveAndWelcomeRequest,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> ApproveAndWelcomeResponse:
    """
    Approve a pending pilot and send the one-time welcome email
    (manifest link + setup steps + starter CSV).
    """
    _require_admin_key(x_admin_key)
    return admin_svc.approve_and_welcome(payload)
