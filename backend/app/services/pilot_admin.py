"""Admin: approve pilot profile + send one-time welcome email."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import structlog
from fastapi import HTTPException, status

from backend.app.config import Settings, get_settings
from backend.app.core.supabase import SupabaseNotConfiguredError, get_service_supabase
from backend.app.schemas.pilot_admin import (
    ApproveAndWelcomeRequest,
    ApproveAndWelcomeResponse,
)
from backend.app.services.pilot_welcome_email import (
    WelcomeEmailError,
    send_pilot_welcome_email,
)

logger = structlog.get_logger(__name__)

WBP_ADMIN_CONFIG = "WBP-ADMIN-CONFIG"
WBP_ADMIN_NOT_FOUND = "WBP-ADMIN-NOT-FOUND"
WBP_ADMIN_EMAIL = "WBP-ADMIN-EMAIL"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def approve_and_welcome(
    payload: ApproveAndWelcomeRequest,
    *,
    settings: Settings | None = None,
) -> ApproveAndWelcomeResponse:
    """
    Set profiles.approval_status=APPROVED and send welcome email once.

    Idempotent: if welcome_email_sent_at is set and force_resend is false,
    skips the email but still ensures APPROVED.
    """
    cfg = settings or get_settings()
    email_norm = str(payload.email).strip().lower()

    try:
        client = get_service_supabase()
    except SupabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": WBP_ADMIN_CONFIG,
                "message": "Supabase is not configured (missing URL/service role).",
            },
        ) from exc

    res = (
        client.table("profiles")
        .select("id,email,approval_status,welcome_email_sent_at,role")
        .ilike("email", email_norm)
        .limit(1)
        .execute()
    )
    rows = list(res.data or [])
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": WBP_ADMIN_NOT_FOUND,
                "message": (
                    f"No profiles row for {email_norm}. "
                    "Ask the pilot to register at /request-access first."
                ),
            },
        )

    profile = dict(rows[0])
    advisor_id = UUID(str(profile["id"]))
    now = _now_iso()

    try:
        client.table("profiles").update(
            {
                "approval_status": "APPROVED",
                "updated_at": now,
            }
        ).eq("id", str(advisor_id)).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "pilot_admin_approve_failed",
            error=str(exc),
            advisor_id=str(advisor_id),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": WBP_ADMIN_CONFIG,
                "message": "Could not update profiles.approval_status.",
            },
        ) from exc

    already_sent = profile.get("welcome_email_sent_at") is not None
    if already_sent and not payload.force_resend:
        logger.info(
            "pilot_welcome_email_skipped",
            advisor_id=str(advisor_id),
            email=email_norm,
        )
        return ApproveAndWelcomeResponse(
            advisor_id=advisor_id,
            email=email_norm,
            approval_status="APPROVED",
            email_sent=False,
            email_skipped=True,
            skip_reason="already_sent",
            message=(
                "Pilot approved. Welcome email was already sent earlier "
                "(use force_resend to send again)."
            ),
        )

    try:
        send_pilot_welcome_email(to_email=email_norm, settings=cfg)
    except WelcomeEmailError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": WBP_ADMIN_EMAIL,
                "message": (
                    f"Pilot approved, but welcome email failed: {exc.message}"
                ),
            },
        ) from exc

    try:
        client.table("profiles").update(
            {
                "welcome_email_sent_at": now,
                "updated_at": now,
            }
        ).eq("id", str(advisor_id)).execute()
    except Exception as exc:  # noqa: BLE001
        # Email already delivered — log but do not fail the admin action hard.
        logger.error(
            "pilot_welcome_sent_at_update_failed",
            error=str(exc),
            advisor_id=str(advisor_id),
        )

    return ApproveAndWelcomeResponse(
        advisor_id=advisor_id,
        email=email_norm,
        approval_status="APPROVED",
        email_sent=True,
        email_skipped=False,
        skip_reason=None,
        message="Pilot approved and welcome email sent.",
    )
