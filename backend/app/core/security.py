"""JWT validation, XSS sanitization, and auth dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

import bleach
import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from backend.app.config import Settings, get_settings

logger = structlog.get_logger(__name__)
_bearer = HTTPBearer(auto_error=False)

_ALLOWED_TAGS: list[str] = []
_ALLOWED_ATTRS: dict[str, list[str]] = {}


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Authenticated advisor context derived from a Supabase Auth JWT."""

    advisor_id: UUID
    email: str | None
    role: str | None
    raw_claims: dict[str, Any]
    approval_status: str | None = None


def sanitize_for_dom(text: str) -> str:
    """Strip HTML/JS from LLM or user text before Excel/Streamlit render."""
    return bleach.clean(text, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True)


def decode_supabase_jwt(token: str, settings: Settings) -> dict[str, Any]:
    """
    Verify Supabase Auth JWT using the project JWT secret.

    Production hardening may switch to JWKS; scaffold uses HS256 secret.
    """
    if not settings.supabase_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SUPABASE_JWT_SECRET is not configured",
        )
    try:
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            options={"verify_aud": True},
        )
    except JWTError as exc:
        logger.warning("jwt_validation_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from exc


def _bypass_allowed(settings: Settings) -> bool:
    """DEV_AUTH_BYPASS is local only — never in production."""
    if settings.environment.lower() == "production":
        return False
    return bool(settings.dev_auth_bypass)


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer),
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthContext:
    # MVP local demo: allow requests without JWT when explicitly enabled
    if _bypass_allowed(settings) and credentials is None:
        return AuthContext(
            advisor_id=UUID(settings.dev_advisor_id),
            email="dev@localhost",
            role="advisor",
            raw_claims={"sub": settings.dev_advisor_id, "bypass": True},
            approval_status="APPROVED",
        )

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization Bearer token",
        )
    claims = decode_supabase_jwt(credentials.credentials, settings)
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject",
        )
    try:
        advisor_id = UUID(str(sub))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token subject is not a valid UUID",
        ) from exc

    # Phase 1 pilot gate: must be APPROVED advisor (skip when local bypass JWT path)
    approval_status: str | None = "APPROVED"
    role = claims.get("role")
    if not (_bypass_allowed(settings) and claims.get("bypass")):
        from backend.app.services.pilot_auth import require_approved_profile

        try:
            profile = require_approved_profile(advisor_id)
            approval_status = str(profile.get("approval_status"))
            role = str(profile.get("role") or role or "advisor")
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — profiles table may be missing locally
            if settings.environment.lower() == "production" or settings.require_pilot_approval:
                logger.warning("pilot_profile_lookup_failed", error=str(exc))
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Pilot approval service unavailable",
                ) from exc
            logger.warning(
                "pilot_profile_lookup_skipped",
                error=str(exc),
                hint="Apply migration_pilot_profiles.sql for approval gate",
            )
            approval_status = None

    return AuthContext(
        advisor_id=advisor_id,
        email=claims.get("email"),
        role=str(role) if role else "advisor",
        raw_claims=claims,
        approval_status=approval_status,
    )
