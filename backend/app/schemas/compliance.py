"""Compliance investigation & chat Pydantic contracts (Phase 1)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class AuditSourceType(str, Enum):
    APPLICATION_EVENT = "application_event"
    AGENT_TELEMETRY = "agent_telemetry"


class ChatMessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


# --- Audit events ------------------------------------------------------------


class AuditEventCreate(BaseModel):
    """Insert payload for immutable audit_events rows."""

    source_type: AuditSourceType
    source_name: str = Field(..., min_length=1, max_length=128)
    event_action: str = Field(..., min_length=1, max_length=128)
    event_type: str | None = Field(default=None, max_length=128)
    advisor_id: UUID | None = None
    session_id: str | None = Field(default=None, max_length=128)
    actor_id: UUID | None = None
    entity_refs: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    normalized_params: dict[str, Any] = Field(default_factory=dict)


class AuditEventRead(BaseModel):
    id: UUID
    source_type: AuditSourceType
    source_name: str
    event_action: str
    event_type: str | None = None
    advisor_id: UUID | None = None
    session_id: str | None = None
    actor_id: UUID | None = None
    created_at: datetime
    entity_refs: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    normalized_params: dict[str, Any] = Field(default_factory=dict)


class AuditEventQuery(BaseModel):
    """Filter set for querying the audit corpus."""

    audit_event_id: UUID | None = None
    source_type: AuditSourceType | None = None
    source_name: str | None = None
    event_action: str | None = None
    session_id: str | None = None
    advisor_id: UUID | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    entity_ticker: str | None = None
    entity_portfolio_id: UUID | None = None
    entity_client_alias: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class AuditEventListResponse(BaseModel):
    events: list[AuditEventRead]
    count: int


# --- Citations / query response ---------------------------------------------


class ComplianceCitation(BaseModel):
    """Structured citation linking an answer to audit + policy sources."""

    audit_event_id: UUID | None = None
    source_type: AuditSourceType | None = None
    source_name: str | None = None
    rule_id: UUID | None = None
    rule_name: str | None = None
    config_key: str | None = None
    config_version: int | None = None
    note: str | None = None


class ComplianceQueryRequest(BaseModel):
    """Direct query (non-chat) against the audit corpus with cite."""

    question: str | None = Field(default=None, max_length=4000)
    filters: AuditEventQuery = Field(default_factory=AuditEventQuery)


class ComplianceQueryResponse(BaseModel):
    answer: str
    events: list[AuditEventRead]
    citations: list[ComplianceCitation]
    normalized_query_params: dict[str, Any] = Field(default_factory=dict)
    intermediate_steps: list[dict[str, Any]] = Field(default_factory=list)
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)


# --- Chat sessions / messages / traces --------------------------------------


class ChatSessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=256)


class ChatSessionRead(BaseModel):
    id: UUID
    advisor_id: UUID
    title: str | None = None
    created_at: datetime
    updated_at: datetime


class ChatMessageRead(BaseModel):
    id: UUID
    session_id: UUID
    role: ChatMessageRole
    content: str
    created_at: datetime


class ChatTraceRead(BaseModel):
    id: UUID
    session_id: UUID
    message_id: UUID
    raw_prompt: str
    normalized_query_params: dict[str, Any] = Field(default_factory=dict)
    intermediate_steps: list[dict[str, Any]] = Field(default_factory=list)
    final_response: str
    citations: list[ComplianceCitation] = Field(default_factory=list)
    created_at: datetime


class ChatSessionDetail(BaseModel):
    session: ChatSessionRead
    messages: list[ChatMessageRead]
    traces: list[ChatTraceRead] = Field(default_factory=list)


class ChatMessageCreate(BaseModel):
    """User question for the Phase-1 deterministic compliance pipeline."""

    content: str = Field(..., min_length=1, max_length=4000)


class ChatTurnResponse(BaseModel):
    """Assistant turn after query → cite pipeline."""

    user_message: ChatMessageRead
    assistant_message: ChatMessageRead
    trace: ChatTraceRead
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)


class TraceExportPayload(BaseModel):
    """Session export for download (JSON / markdown)."""

    session: ChatSessionRead
    messages: list[ChatMessageRead]
    traces: list[ChatTraceRead]
    exported_at: datetime
    format: str = "json"
    markdown: str | None = None
