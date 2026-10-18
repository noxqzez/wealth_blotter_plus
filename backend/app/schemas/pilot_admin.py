"""Admin pilot approval + welcome email contracts."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from backend.app.schemas.pilot_auth import ApprovalStatus


class ApproveAndWelcomeRequest(BaseModel):
    email: EmailStr
    force_resend: bool = Field(
        default=False,
        description="If true, send again even when welcome_email_sent_at is set.",
    )


class ApproveAndWelcomeResponse(BaseModel):
    advisor_id: UUID
    email: str
    approval_status: ApprovalStatus
    email_sent: bool
    email_skipped: bool = False
    skip_reason: str | None = None
    message: str
