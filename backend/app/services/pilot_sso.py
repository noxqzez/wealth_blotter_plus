"""SSO (Supabase OAuth) helpers — gated by SSO_ENABLED (default off)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import structlog
from fastapi import HTTPException, status

from backend.app.config import Settings, get_settings
from backend.app.core.security import decode_supabase_jwt
from backend.app.core.supabase import SupabaseNotConfiguredError, get_service_supabase
from backend.app.schemas.pilot_auth import (
    LoginResponse,
    SsoAuthorizeResponse,
    SsoStatusResponse,
)
from backend.app.services.pilot_auth import (
    WBP_AUTH_INVALID,
    WBP_AUTH_PENDING,
    _now_iso,
    fetch_profile,
    require_approved_profile,
)

logger = structlog.get_logger(__name__)

WBP_AUTH_SSO_DISABLED = "WBP-AUTH-SSO-DISABLED"
WBP_AUTH_SSO_PROVIDER = "WBP-AUTH-SSO-PROVIDER"

# Supabase Auth provider ids we allow when listed in SSO_PROVIDERS.
_ALLOWED_PROVIDER_ALIASES: dict[str, str] = {
    "google": "google",
    "azure": "azure",
    "azure_ad": "azure",
    "entra": "azure",
    "github": "github",
}


def sso_status(settings: Settings | None = None) -> SsoStatusResponse:
    cfg = settings or get_settings()
    enabled = bool(cfg.sso_enabled)
    providers = cfg.sso_provider_list if enabled else []
    return SsoStatusResponse(
        enabled=enabled,
        providers=providers,
        require_approval=bool(cfg.sso_require_approval),
    )


def require_sso_enabled(settings: Settings | None = None) -> Settings:
    cfg = settings or get_settings()
    if not cfg.sso_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": WBP_AUTH_SSO_DISABLED,
                "message": (
                    "SSO is disabled on this server. "
                    "Use email/password registration and sign-in."
                ),
            },
        )
    return cfg


def normalize_provider(provider: str, *, settings: Settings) -> str:
    raw = (provider or "").strip().lower()
    canonical = _ALLOWED_PROVIDER_ALIASES.get(raw)
    if canonical is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": WBP_AUTH_SSO_PROVIDER,
                "message": f"Unsupported SSO provider: {provider!r}.",
            },
        )
    allowed = settings.sso_provider_list
    if allowed and canonical not in allowed and raw not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": WBP_AUTH_SSO_PROVIDER,
                "message": (
                    f"Provider {canonical!r} is not enabled. "
                    f"Allowed: {', '.join(allowed)}."
                ),
            },
        )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": WBP_AUTH_SSO_PROVIDER,
                "message": "SSO_PROVIDERS is empty. Configure providers before enabling SSO.",
            },
        )
    return canonical


def build_redirect_to(*, public_base: str) -> str:
    base = public_base.rstrip("/")
    return f"{base}/auth/sso/callback"


def authorize_sso(
    *,
    provider: str,
    public_base: str,
    settings: Settings | None = None,
) -> SsoAuthorizeResponse:
    """Return Supabase authorize URL. Rejects when SSO_ENABLED=false."""
    cfg = require_sso_enabled(settings)
    if not cfg.supabase_url or not cfg.supabase_anon_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "Auth is not configured on this server.",
            },
        )
    canonical = normalize_provider(provider, settings=cfg)
    redirect_to = build_redirect_to(public_base=public_base)
    query = urlencode(
        {
            "provider": canonical,
            "redirect_to": redirect_to,
        }
    )
    authorize_url = (
        f"{cfg.supabase_url.rstrip('/')}/auth/v1/authorize?{query}"
    )
    return SsoAuthorizeResponse(
        provider=canonical,
        authorize_url=authorize_url,
        redirect_to=redirect_to,
    )


def _email_domain(email: str) -> str:
    parts = email.strip().lower().split("@", 1)
    return parts[1] if len(parts) == 2 else ""


def resolve_sso_approval_status(*, email: str, settings: Settings) -> str:
    """PENDING unless domain auto-approve is explicitly allowed."""
    if settings.sso_require_approval:
        return "PENDING_APPROVAL"
    domain = _email_domain(email)
    if domain and domain in settings.sso_auto_approve_domain_list:
        return "APPROVED"
    return "PENDING_APPROVAL"


def ensure_sso_profile(
    *,
    advisor_id: UUID,
    email: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Create profiles row on first SSO login if missing."""
    cfg = settings or get_settings()
    existing = fetch_profile(advisor_id)
    if existing is not None:
        return existing

    email_norm = email.strip().lower()
    approval = resolve_sso_approval_status(email=email_norm, settings=cfg)
    now = _now_iso()
    try:
        client = get_service_supabase()
    except SupabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "Supabase is not configured on this server.",
            },
        ) from exc

    row = {
        "id": str(advisor_id),
        "email": email_norm or f"{advisor_id}@sso.local",
        "role": "advisor",
        "approval_status": approval,
        "created_at": now,
        "updated_at": now,
    }
    try:
        client.table("profiles").upsert(row, on_conflict="id").execute()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "sso_profile_upsert_failed",
            error=str(exc),
            advisor_id=str(advisor_id),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": WBP_AUTH_PENDING,
                "message": "Could not create pilot profile for SSO user.",
            },
        ) from exc
    logger.info(
        "sso_profile_created",
        advisor_id=str(advisor_id),
        approval_status=approval,
    )
    created = fetch_profile(advisor_id)
    return created or row


def complete_sso_login(
    *,
    access_token: str,
    settings: Settings | None = None,
) -> LoginResponse:
    """
    Validate SSO JWT, bootstrap profile, return tokens only if APPROVED.
    """
    cfg = require_sso_enabled(settings)
    claims = decode_supabase_jwt(access_token.strip(), cfg)
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "SSO token missing subject.",
            },
        )
    try:
        advisor_id = UUID(str(sub))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": WBP_AUTH_INVALID,
                "message": "SSO token subject is invalid.",
            },
        ) from exc

    email = str(claims.get("email") or "").strip().lower()
    ensure_sso_profile(advisor_id=advisor_id, email=email, settings=cfg)
    profile = require_approved_profile(advisor_id)
    return LoginResponse(
        access_token=access_token.strip(),
        expires_in=None,
        refresh_token=None,
        advisor_id=advisor_id,
        email=str(profile.get("email") or email),
        approval_status=str(profile.get("approval_status")),  # type: ignore[arg-type]
        role="advisor",
    )
