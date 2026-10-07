"""Phase 1 pilot auth endpoints — register / login / me."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.app.core.security import AuthContext, get_current_user
from backend.app.schemas.pilot_auth import (
    LoginRequest,
    LoginResponse,
    MeResponse,
    RegisterRequest,
    RegisterResponse,
)
from backend.app.services import pilot_auth as svc

router = APIRouter()


@router.post("/register", response_model=RegisterResponse)
async def register(payload: RegisterRequest) -> RegisterResponse:
    """Closed signup. Account starts PENDING_APPROVAL (no JWT until approved)."""
    return svc.register_pilot(email=str(payload.email), password=payload.password)


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest) -> LoginResponse:
    """Return JWT only when profiles.approval_status = APPROVED."""
    return svc.login_pilot(email=str(payload.email), password=payload.password)


@router.get("/me", response_model=MeResponse)
async def me(user: AuthContext = Depends(get_current_user)) -> MeResponse:
    """Current pilot profile (requires approved JWT)."""
    return svc.me_from_profile(user.advisor_id, email_fallback=user.email)
