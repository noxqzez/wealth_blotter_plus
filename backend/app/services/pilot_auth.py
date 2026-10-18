"""Pilot registration / login against Supabase Auth + profiles gate."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
import structlog
from fastapi import HTTPException, status

from backend.app.config import Settings, get_settings
from backend.app.core.supabase import get_service_supabase
from backend.app.schemas.pilot_auth import (
    ApprovalStatus,
    LoginResponse,
    MeResponse,
    RegisterResponse,
)

logger = structlog.get_logger(__name__)

WBP_AUTH_PENDING = "WBP-AUTH-PENDING"
WBP_AUTH_REJECTED = "WBP-AUTH-REJECTED"
WBP_AUTH_INVALID = "WBP-AUTH-INVALID"
WBP_AUTH_REGISTER = "WBP-AUTH-REGISTER"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_profile(advisor_id: UUID) -> dict[str, Any] | None:
    client = get_service_supabase()
    res = (
        client.table("profiles")
        .select("id,email,role,approval_status")
        .eq("id", str(advisor_id))
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    return dict(res.data[0])


def require_approved_profile(advisor_id: UUID) -> dict[str, Any]:
    profile = fetch_profile(advisor_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": WBP_AUTH_PENDING,
                "message": "No pilot profile found. Register first and wait for approval.",
            },
        )
    status_val = str(profile.get("approval_status") or "")
    if status_val == "REJECTED":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": WBP_AUTH_REJECTED,
                "message": "This pilot account was rejected. Contact the program admin.",
            },
        )
    if status_val != "APPROVED":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": WBP_AUTH_PENDING,
                "message": (
                    "Account is pending approval. "
                    "You will be able to sign in after an admin sets approval_status=APPROVED."
                ),
            },
        )
    role = str(profile.get("role") or "")
    if role != "advisor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "Phase 1 pilots must use an advisor role.",
            },
        )
    return profile


def register_pilot(*, email: str, password: str) -> RegisterResponse:
    """Create Auth user + PENDING profiles row. Does not return a JWT."""
    from backend.app.core.supabase import SupabaseNotConfiguredError

    email_norm = email.strip().lower()
    try:
        client = get_service_supabase()
    except SupabaseNotConfiguredError as exc:
        logger.error("pilot_register_supabase_not_configured", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": WBP_AUTH_REGISTER,
                "message": (
                    "Server auth is not configured (missing Supabase URL/service role). "
                    "Check Render environment variables."
                ),
            },
        ) from exc

    try:
        created = client.auth.admin.create_user(
            {
                "email": email_norm,
                "password": password,
                "email_confirm": True,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("pilot_register_auth_failed", error=str(exc), email=email_norm)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": WBP_AUTH_REGISTER,
                "message": "Could not register. Email may already be in use.",
            },
        ) from exc

    user = getattr(created, "user", None) or created
    user_id = getattr(user, "id", None)
    if user_id is None and isinstance(user, dict):
        user_id = user.get("id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": WBP_AUTH_REGISTER,
                "message": "Registration failed (no user id).",
            },
        )
    advisor_id = UUID(str(user_id))
    now = _now_iso()
    try:
        client.table("profiles").upsert(
            {
                "id": str(advisor_id),
                "email": email_norm,
                "role": "advisor",
                "approval_status": "PENDING_APPROVAL",
                "created_at": now,
                "updated_at": now,
            },
            on_conflict="id",
        ).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "pilot_register_profile_failed",
            error=str(exc),
            advisor_id=str(advisor_id),
            email=email_norm,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": WBP_AUTH_REGISTER,
                "message": (
                    "Auth user created but profiles table write failed. "
                    "Apply migration_pilot_profiles.sql in Supabase, then retry "
                    "(or ask an admin to insert your profiles row)."
                ),
            },
        ) from exc
    logger.info("pilot_registered", advisor_id=str(advisor_id), email=email_norm)
    return RegisterResponse(
        advisor_id=advisor_id,
        email=email_norm,
        approval_status="PENDING_APPROVAL",
        message=(
            "Registration received. An admin must set profiles.approval_status "
            "to APPROVED before you can log in."
        ),
    )


def login_pilot(*, email: str, password: str) -> LoginResponse:
    """Password grant; only APPROVED advisors receive a token."""
    settings = get_settings()
    email_norm = email.strip().lower()
    if not settings.supabase_url or not settings.supabase_anon_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "Auth is not configured on this server.",
            },
        )

    url = f"{settings.supabase_url.rstrip('/')}/auth/v1/token?grant_type=password"
    try:
        with httpx.Client(timeout=30.0) as http:
            resp = http.post(
                url,
                headers={
                    "apikey": settings.supabase_anon_key,
                    "Content-Type": "application/json",
                },
                json={"email": email_norm, "password": password},
            )
    except httpx.HTTPError as exc:
        logger.warning("pilot_login_http_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "Auth service unreachable.",
            },
        ) from exc

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "Invalid email or password.",
            },
        )
    body = resp.json()
    access = body.get("access_token")
    if not access:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "Invalid email or password.",
            },
        )
    user = body.get("user") or {}
    uid = user.get("id")
    if not uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "Invalid email or password.",
            },
        )
    advisor_id = UUID(str(uid))
    profile = require_approved_profile(advisor_id)
    return LoginResponse(
        access_token=str(access),
        expires_in=body.get("expires_in"),
        refresh_token=body.get("refresh_token"),
        advisor_id=advisor_id,
        email=str(profile.get("email") or email_norm),
        approval_status=str(profile.get("approval_status")),  # type: ignore[arg-type]
        role="advisor",
    )


def refresh_pilot(*, refresh_token: str) -> LoginResponse:
    """Exchange Supabase refresh_token for a new access_token (keeps Excel sessions alive)."""
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_anon_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "Auth is not configured on this server.",
            },
        )
    token = refresh_token.strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "Missing refresh token.",
            },
        )
    url = f"{settings.supabase_url.rstrip('/')}/auth/v1/token?grant_type=refresh_token"
    try:
        with httpx.Client(timeout=30.0) as http:
            resp = http.post(
                url,
                headers={
                    "apikey": settings.supabase_anon_key,
                    "Content-Type": "application/json",
                },
                json={"refresh_token": token},
            )
    except httpx.HTTPError as exc:
        logger.warning("pilot_refresh_http_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "Auth service unreachable.",
            },
        ) from exc

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "Session expired — sign in again.",
            },
        )
    body = resp.json()
    access = body.get("access_token")
    if not access:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "Session expired — sign in again.",
            },
        )
    user = body.get("user") or {}
    uid = user.get("id")
    if not uid:
        # Decode sub from access token via service path if user omitted.
        from backend.app.core.security import decode_supabase_jwt

        try:
            claims = decode_supabase_jwt(str(access), settings)
            uid = claims.get("sub")
        except HTTPException:
            uid = None
    if not uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "Session expired — sign in again.",
            },
        )
    advisor_id = UUID(str(uid))
    profile = require_approved_profile(advisor_id)
    return LoginResponse(
        access_token=str(access),
        expires_in=body.get("expires_in"),
        refresh_token=str(body.get("refresh_token") or token),
        advisor_id=advisor_id,
        email=str(profile.get("email") or user.get("email") or ""),
        approval_status=str(profile.get("approval_status")),  # type: ignore[arg-type]
        role="advisor",
    )


def me_from_profile(advisor_id: UUID, *, email_fallback: str | None) -> MeResponse:
    profile = fetch_profile(advisor_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": WBP_AUTH_PENDING,
                "message": "Profile not found.",
            },
        )
    return MeResponse(
        advisor_id=advisor_id,
        email=str(profile.get("email") or email_fallback or ""),
        role="advisor",
        approval_status=str(profile.get("approval_status")),  # type: ignore[arg-type]
    )
