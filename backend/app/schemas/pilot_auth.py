"""Phase 1 pilot auth / approval / BYOK contracts."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


ApprovalStatus = Literal["PENDING_APPROVAL", "APPROVED", "REJECTED"]
PilotRole = Literal["advisor"]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)


class RegisterResponse(BaseModel):
    advisor_id: UUID
    email: str
    approval_status: ApprovalStatus
    message: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int | None = None
    refresh_token: str | None = None
    advisor_id: UUID
    email: str
    approval_status: ApprovalStatus
    role: PilotRole


class MeResponse(BaseModel):
    advisor_id: UUID
    email: str | None
    role: PilotRole
    approval_status: ApprovalStatus


class SsoStatusResponse(BaseModel):
    """Public SSO capability probe — safe when SSO is disabled."""

    enabled: bool
    providers: list[str]
    require_approval: bool


class SsoAuthorizeResponse(BaseModel):
    provider: str
    authorize_url: str
    redirect_to: str


class SsoCompleteRequest(BaseModel):
    """Client posts the Supabase access_token after OAuth redirect."""

    access_token: str = Field(..., min_length=20, max_length=8192)
