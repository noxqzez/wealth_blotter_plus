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
