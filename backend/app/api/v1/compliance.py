"""Compliance investigation API (Phase 1: query + cite + chat)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app.core.security import AuthContext, get_current_user
from backend.app.schemas.compliance import (
    AuditEventListResponse,
    AuditEventQuery,
    AuditEventRead,
    AuditSourceType,
    ChatMessageCreate,
    ChatSessionCreate,
    ChatSessionDetail,
    ChatSessionRead,
    ChatTurnResponse,
    ComplianceQueryRequest,
    ComplianceQueryResponse,
    TraceExportPayload,
)
from backend.app.services import audit_events as audit_svc
from backend.app.services import compliance_chat as chat_svc

router = APIRouter()


@router.post("/chat/sessions", response_model=ChatSessionRead)
async def create_chat_session(
    payload: ChatSessionCreate,
    user: AuthContext = Depends(get_current_user),
) -> ChatSessionRead:
    return await chat_svc.create_session(user.advisor_id, payload)


@router.get("/chat/sessions", response_model=list[ChatSessionRead])
async def list_chat_sessions(
    user: AuthContext = Depends(get_current_user),
) -> list[ChatSessionRead]:
    return await chat_svc.list_sessions(user.advisor_id)


@router.get("/chat/sessions/{session_id}", response_model=ChatSessionDetail)
async def get_chat_session(
    session_id: UUID,
    user: AuthContext = Depends(get_current_user),
) -> ChatSessionDetail:
    detail = await chat_svc.get_session_detail(session_id, user.advisor_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return detail


@router.post(
    "/chat/sessions/{session_id}/messages",
    response_model=ChatTurnResponse,
)
async def post_chat_message(
    session_id: UUID,
    payload: ChatMessageCreate,
    user: AuthContext = Depends(get_current_user),
) -> ChatTurnResponse:
    try:
        return await chat_svc.post_chat_message(session_id, user.advisor_id, payload)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        ) from exc


@router.get(
    "/chat/sessions/{session_id}/export",
    response_model=TraceExportPayload,
)
async def export_chat_session(
    session_id: UUID,
    user: AuthContext = Depends(get_current_user),
) -> TraceExportPayload:
    payload = await chat_svc.export_session(session_id, user.advisor_id)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return payload


@router.post("/query", response_model=ComplianceQueryResponse)
async def compliance_query(
    payload: ComplianceQueryRequest,
    _user: AuthContext = Depends(get_current_user),
) -> ComplianceQueryResponse:
    """Non-chat query + cite against audit_events / trading_rules / configs."""
    return await chat_svc.execute_direct_query(payload)


@router.get("/audit-events", response_model=AuditEventListResponse)
async def list_audit_events(
    _user: AuthContext = Depends(get_current_user),
    audit_event_id: UUID | None = None,
    source_type: AuditSourceType | None = None,
    source_name: str | None = None,
    event_action: str | None = None,
    session_id: str | None = None,
    entity_ticker: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> AuditEventListResponse:
    filters = AuditEventQuery(
        audit_event_id=audit_event_id,
        source_type=source_type,
        source_name=source_name,
        event_action=event_action,
        session_id=session_id,
        entity_ticker=entity_ticker,
        limit=limit,
    )
    events = await audit_svc.query_audit_events(filters)
    return AuditEventListResponse(events=events, count=len(events))


@router.get("/audit-events/{event_id}", response_model=AuditEventRead)
async def get_audit_event(
    event_id: UUID,
    _user: AuthContext = Depends(get_current_user),
) -> AuditEventRead:
    event = await audit_svc.get_audit_event(event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit event not found")
    return event
