"""Phase 1 pilot auth endpoints — register / login / me / SSO (default off)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from backend.app.config import Settings, get_settings
from backend.app.core.security import AuthContext, get_current_user
from backend.app.schemas.pilot_auth import (
    LoginRequest,
    LoginResponse,
    MeResponse,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    SsoAuthorizeResponse,
    SsoCompleteRequest,
    SsoStatusResponse,
)
from backend.app.services import pilot_auth as svc
from backend.app.services import pilot_sso as sso_svc

router = APIRouter()


def _public_base(request: Request, settings: Settings) -> str:
    """Prefer forwarded host (Render) so OAuth redirect_to matches the public URL."""
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[
        0
    ].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(
        ","
    )[0].strip()
    if host:
        return f"{proto}://{host}"
    return str(request.base_url).rstrip("/")


@router.post("/register", response_model=RegisterResponse)
async def register(payload: RegisterRequest) -> RegisterResponse:
    """Closed signup. Account starts PENDING_APPROVAL (no JWT until approved)."""
    return svc.register_pilot(email=str(payload.email), password=payload.password)


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest) -> LoginResponse:
    """Return JWT only when profiles.approval_status = APPROVED."""
    return svc.login_pilot(email=str(payload.email), password=payload.password)


@router.post("/refresh", response_model=LoginResponse)
async def refresh(payload: RefreshRequest) -> LoginResponse:
    """Renew access_token from refresh_token so Excel sessions stay open."""
    return svc.refresh_pilot(refresh_token=payload.refresh_token)


@router.get("/me", response_model=MeResponse)
async def me(user: AuthContext = Depends(get_current_user)) -> MeResponse:
    """Current pilot profile (requires approved JWT)."""
    return svc.me_from_profile(user.advisor_id, email_fallback=user.email)


@router.get("/sso/status", response_model=SsoStatusResponse)
async def sso_status(
    settings: Settings = Depends(get_settings),
) -> SsoStatusResponse:
    """Public probe — returns enabled=false when SSO is off (default)."""
    return sso_svc.sso_status(settings)


@router.get("/sso/authorize", response_model=SsoAuthorizeResponse)
async def sso_authorize(
    request: Request,
    provider: str = Query(..., min_length=2, max_length=32),
    settings: Settings = Depends(get_settings),
) -> SsoAuthorizeResponse:
    """Build Supabase OAuth URL. 403 when SSO_ENABLED=false."""
    return sso_svc.authorize_sso(
        provider=provider,
        public_base=_public_base(request, settings),
        settings=settings,
    )


@router.post("/sso/complete", response_model=LoginResponse)
async def sso_complete(
    payload: SsoCompleteRequest,
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    """
    Finish SSO after IdP redirect. Bootstraps profiles row; JWT only if APPROVED.
    403 when SSO_ENABLED=false.
    """
    return sso_svc.complete_sso_login(
        access_token=payload.access_token,
        settings=settings,
    )
