"""Audit event writer + query service (compliance corpus)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from backend.app.core.supabase import get_service_supabase
from backend.app.schemas.compliance import (
    AuditEventCreate,
    AuditEventQuery,
    AuditEventRead,
    AuditSourceType,
)
from backend.app.services.anonymize import anonymize_entity_refs, anonymize_payload

logger = structlog.get_logger(__name__)

SOURCE_PORTFOLIO_API = "portfolio_api_v1"
SOURCE_AGENT_TRACE = "agent_trace_run"  # Phase 2 writer interface contract


def _row_to_event(row: dict[str, Any]) -> AuditEventRead:
    return AuditEventRead.model_validate(row)


def insert_audit_event_row(
    *,
    source_type: str,
    source_name: str,
    event_action: str,
    event_type: str | None = None,
    advisor_id: UUID | None = None,
    actor_id: UUID | None = None,
    session_id: str | None = None,
    entity_refs: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    normalized_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Sync insert into ``audit_events`` with mandatory anonymization.

    Shared by async API writers and sync scenario-step telemetry.
    Returns the inserted row dict.
    """
    client = get_service_supabase()
    body: dict[str, Any] = {
        "source_type": source_type,
        "source_name": source_name,
        "event_action": event_action,
        "event_type": event_type,
        "advisor_id": str(advisor_id) if advisor_id else None,
        "session_id": session_id,
        "actor_id": str(actor_id) if actor_id else None,
        "entity_refs": anonymize_entity_refs(entity_refs or {}),
        "payload": anonymize_payload(payload or {}),
        "normalized_params": anonymize_payload(normalized_params or {}),
    }
    result = client.table("audit_events").insert(body).execute()
    if not result.data:
        raise RuntimeError("audit_events insert returned no row")
    return result.data[0]


async def write_audit_event(event: AuditEventCreate) -> AuditEventRead:
    """
    Insert an immutable audit_events row via service role.

    Always anonymizes entity_refs / payload before persist.
    """
    created = _row_to_event(
        insert_audit_event_row(
            source_type=event.source_type.value,
            source_name=event.source_name,
            event_action=event.event_action,
            event_type=event.event_type,
            advisor_id=event.advisor_id,
            actor_id=event.actor_id,
            session_id=event.session_id,
            entity_refs=event.entity_refs,
            payload=event.payload,
            normalized_params=event.normalized_params,
        )
    )
    logger.info(
        "audit_event_written",
        audit_event_id=str(created.id),
        source_type=created.source_type.value,
        source_name=created.source_name,
        event_action=created.event_action,
    )
    return created


async def write_application_event(
    *,
    source_name: str,
    event_action: str,
    advisor_id: UUID | None = None,
    actor_id: UUID | None = None,
    session_id: str | None = None,
    event_type: str | None = None,
    entity_refs: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    normalized_params: dict[str, Any] | None = None,
) -> AuditEventRead:
    """Convenience writer for source_type=application_event."""
    return await write_audit_event(
        AuditEventCreate(
            source_type=AuditSourceType.APPLICATION_EVENT,
            source_name=source_name,
            event_action=event_action,
            event_type=event_type,
            advisor_id=advisor_id,
            actor_id=actor_id or advisor_id,
            session_id=session_id,
            entity_refs=entity_refs or {},
            payload=payload or {},
            normalized_params=normalized_params or {},
        )
    )


async def get_audit_event(event_id: UUID) -> AuditEventRead | None:
    client = get_service_supabase()
    result = (
        client.table("audit_events")
        .select("*")
        .eq("id", str(event_id))
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    return _row_to_event(result.data[0])


async def query_audit_events(filters: AuditEventQuery) -> list[AuditEventRead]:
    """Filter audit_events by time, ids, source, action, and entity refs."""
    client = get_service_supabase()

    if filters.audit_event_id is not None:
        event = await get_audit_event(filters.audit_event_id)
        return [event] if event else []

    q = client.table("audit_events").select("*")

    if filters.source_type is not None:
        q = q.eq("source_type", filters.source_type.value)
    if filters.source_name:
        q = q.eq("source_name", filters.source_name)
    if filters.event_action:
        q = q.eq("event_action", filters.event_action)
    if filters.session_id:
        q = q.eq("session_id", filters.session_id)
    if filters.advisor_id is not None:
        q = q.eq("advisor_id", str(filters.advisor_id))
    if filters.created_after is not None:
        q = q.gte("created_at", _iso(filters.created_after))
    if filters.created_before is not None:
        q = q.lte("created_at", _iso(filters.created_before))

    # PostgREST JSON containment for entity_refs keys
    contains: dict[str, Any] = {}
    if filters.entity_ticker:
        contains["ticker"] = filters.entity_ticker.strip().upper()
    if filters.entity_portfolio_id is not None:
        contains["portfolio_id"] = str(filters.entity_portfolio_id)
    if filters.entity_client_alias:
        contains["client_alias"] = filters.entity_client_alias.strip()
    if contains:
        q = q.contains("entity_refs", contains)

    result = q.order("created_at", desc=True).limit(filters.limit).execute()
    return [_row_to_event(row) for row in (result.data or [])]


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        return dt.isoformat() + "Z"
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Phase 2 handoff contract (crew_ai_coding owns the writer implementation)
# ---------------------------------------------------------------------------
#
# Interface: AgentTelemetryWriter.write_trace_run(...)
#   source_type = "agent_telemetry"
#   source_name = "agent_trace_run" (or per-crew name)
#   Must call write_audit_event / anonymize before any LLM payload.
#   Do NOT edit backend/app/agents/ from the compliance workstream.
#
class AgentTelemetryWriterContract:
    """
    Documented interface for Phase 2 agent telemetry writers.

    crew_ai_coding should implement a thin adapter that calls
    ``write_audit_event`` with ``AuditSourceType.AGENT_TELEMETRY``.
    """

    async def write_trace_run(
        self,
        *,
        event_action: str,
        advisor_id: UUID | None,
        session_id: str | None,
        entity_refs: dict[str, Any],
        payload: dict[str, Any],
        normalized_params: dict[str, Any] | None = None,
    ) -> AuditEventRead:
        return await write_audit_event(
            AuditEventCreate(
                source_type=AuditSourceType.AGENT_TELEMETRY,
                source_name=SOURCE_AGENT_TRACE,
                event_action=event_action,
                advisor_id=advisor_id,
                actor_id=advisor_id,
                session_id=session_id,
                entity_refs=entity_refs,
                payload=payload,
                normalized_params=normalized_params or {},
            )
        )
