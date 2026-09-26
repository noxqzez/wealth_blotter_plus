"""Phase-1 deterministic compliance chat orchestrator + export."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import structlog

from backend.app.core.security import sanitize_for_dom
from backend.app.core.supabase import get_service_supabase
from backend.app.schemas.compliance import (
    AuditEventQuery,
    AuditEventRead,
    AuditSourceType,
    ChatMessageCreate,
    ChatMessageRead,
    ChatMessageRole,
    ChatSessionCreate,
    ChatSessionDetail,
    ChatSessionRead,
    ChatTraceRead,
    ChatTurnResponse,
    ComplianceCitation,
    ComplianceQueryRequest,
    ComplianceQueryResponse,
    TraceExportPayload,
)
from backend.app.services.anonymize import anonymize_text
from backend.app.services.audit_events import query_audit_events
from backend.app.services.compliance_cite import cite_audit_events

logger = structlog.get_logger(__name__)

_UUID_RE = re.compile(
    r"\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b"
)
_TICKER_RE = re.compile(r"\b(?:ticker|symbol)\s*[:=]?\s*([A-Z]{1,6}(?:\.[A-Z]{1,3})?)\b", re.I)
_BARE_TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")
_ACTION_RE = re.compile(
    r"\b(portfolio_update|portfolio_flagged|rule_violation|agent_trace)\b", re.I
)


def parse_question_to_filters(question: str) -> AuditEventQuery:
    """
    Deterministic Intake/Plan stub: extract filters from free text.

    Supported Phase-1 patterns:
      - log / event UUID
      - "yesterday" / "today" / "last N hours|days"
      - source_type application_event | agent_telemetry
      - event_action portfolio_update | …
      - ticker=XYZ or common equity tickers in flagged questions
    """
    text = question.strip()
    filters = AuditEventQuery()

    uuid_match = _UUID_RE.search(text)
    if uuid_match:
        filters.audit_event_id = UUID(uuid_match.group(1))

    lower = text.lower()
    now = datetime.now(timezone.utc)
    if "yesterday" in lower:
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        filters.created_after = start
        filters.created_before = end
    elif "today" in lower:
        filters.created_after = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        hours = re.search(r"last\s+(\d+)\s+hours?", lower)
        days = re.search(r"last\s+(\d+)\s+days?", lower)
        if hours:
            filters.created_after = now - timedelta(hours=int(hours.group(1)))
        elif days:
            filters.created_after = now - timedelta(days=int(days.group(1)))

    if "application_event" in lower or "portfolio api" in lower:
        filters.source_type = AuditSourceType.APPLICATION_EVENT
    elif "agent_telemetry" in lower or "agent trace" in lower:
        filters.source_type = AuditSourceType.AGENT_TELEMETRY

    action = _ACTION_RE.search(text)
    if action:
        filters.event_action = action.group(1).lower()
    elif "portfolio" in lower and ("update" in lower or "flag" in lower):
        filters.event_action = "portfolio_update"

    ticker_m = _TICKER_RE.search(text)
    if ticker_m:
        filters.entity_ticker = ticker_m.group(1).upper()
    elif "flagged" in lower or "violate" in lower:
        # Prefer explicit tickers; skip common English noise words
        noise = {
            "WHY",
            "WAS",
            "DOES",
            "LOG",
            "ID",
            "THE",
            "AND",
            "FOR",
            "AGAINST",
            "RULE",
            "WHAT",
            "HOW",
            "DID",
            "THIS",
            "THAT",
            "FROM",
            "WITH",
            "INTO",
            "OVER",
            "LAST",
            "DAYS",
            "HOURS",
            "TODAY",
            "YESTERDAY",
            "PORTFOLIO",
            "UPDATE",
            "EVENT",
            "AUDIT",
            "COMPLIANCE",
        }
        for m in _BARE_TICKER_RE.finditer(text.upper()):
            tok = m.group(1)
            if tok not in noise:
                filters.entity_ticker = tok
                break

    return filters


def _filters_to_dict(filters: AuditEventQuery) -> dict[str, Any]:
    return json.loads(filters.model_dump_json())


def _build_answer(
    question: str,
    events: list[AuditEventRead],
    citations: list[ComplianceCitation],
) -> tuple[str, float]:
    """Deterministic Reason step — structured prose, no LLM."""
    if not events:
        return (
            "No matching audit events found for the parsed filters. "
            "Try a known audit_events.id, 'yesterday portfolio_update', "
            "or source_type=application_event.",
            0.4,
        )

    lines: list[str] = [
        f"Found {len(events)} audit event(s) for: {anonymize_text(question)[:200]}",
        "",
    ]
    violation_notes = [c.note for c in citations if c.note and ("exceed" in c.note.lower())]
    for ev in events[:10]:
        lines.append(
            f"- Log `{ev.id}` | {ev.source_type.value}/{ev.source_name} | "
            f"action={ev.event_action} | at={ev.created_at.isoformat()}"
        )
        refs = ev.entity_refs
        if refs:
            lines.append(f"  entity_refs: {json.dumps(refs, default=str)}")
        if ev.payload.get("clients_updated"):
            lines.append(f"  clients_updated: {ev.payload.get('clients_updated')}")
        if ev.payload.get("positions_count") is not None:
            lines.append(f"  positions_count: {ev.payload.get('positions_count')}")

    if violation_notes:
        lines.append("")
        lines.append("Policy evaluation:")
        for note in violation_notes[:8]:
            lines.append(f"- {note}")
        confidence = 0.9
    else:
        lines.append("")
        lines.append(
            "No hard threshold breach detected against active trading_rules / configs "
            "for these events. See citations for policy context."
        )
        confidence = 0.75

    rule_ids = {str(c.rule_id) for c in citations if c.rule_id}
    config_keys = {c.config_key for c in citations if c.config_key}
    lines.append("")
    lines.append(
        f"Citations: {len(citations)} "
        f"({len(rule_ids)} trading_rules, {len(config_keys)} config keys)."
    )
    return "\n".join(lines), confidence


async def run_compliance_pipeline(
    question: str,
    *,
    explicit_filters: AuditEventQuery | None = None,
) -> ComplianceQueryResponse:
    """
    Phase-1 thin orchestrator mirroring the 5-step crew in code:

    1. Intake  — anonymize prompt
    2. Plan    — parse filters
    3. Query   — audit_events
    4. Reason  — deterministic evaluation
    5. Cite    — trading_rules + configs
    """
    safe_question = anonymize_text(question)
    steps: list[dict[str, Any]] = [
        {"step": "intake", "status": "ok", "detail": "prompt anonymized"},
    ]

    filters = explicit_filters or parse_question_to_filters(safe_question)
    # If caller passed empty filters via ComplianceQueryRequest.filters only,
    # still merge question parse when question present and no explicit id.
    if explicit_filters is not None and question and explicit_filters.audit_event_id is None:
        parsed = parse_question_to_filters(safe_question)
        merged = filters.model_copy()
        for field in (
            "audit_event_id",
            "source_type",
            "source_name",
            "event_action",
            "session_id",
            "created_after",
            "created_before",
            "entity_ticker",
            "entity_portfolio_id",
            "entity_client_alias",
        ):
            if getattr(merged, field) is None and getattr(parsed, field) is not None:
                setattr(merged, field, getattr(parsed, field))
        filters = merged

    params = _filters_to_dict(filters)
    steps.append({"step": "plan", "status": "ok", "normalized_query_params": params})

    events = await query_audit_events(filters)
    steps.append(
        {
            "step": "query",
            "status": "ok",
            "event_ids": [str(e.id) for e in events],
            "count": len(events),
        }
    )

    citations = await cite_audit_events(events)
    steps.append({"step": "cite", "status": "ok", "citation_count": len(citations)})

    answer, confidence = _build_answer(safe_question, events, citations)
    steps.append({"step": "reason", "status": "ok", "confidence_score": confidence})

    return ComplianceQueryResponse(
        answer=sanitize_for_dom(answer),
        events=events,
        citations=citations,
        normalized_query_params=params,
        intermediate_steps=steps,
        confidence_score=confidence,
    )


# --- Session persistence ----------------------------------------------------


def _session_row(row: dict[str, Any]) -> ChatSessionRead:
    return ChatSessionRead.model_validate(row)


def _message_row(row: dict[str, Any]) -> ChatMessageRead:
    return ChatMessageRead.model_validate(row)


def _trace_row(row: dict[str, Any]) -> ChatTraceRead:
    citations_raw = row.get("citations") or []
    citations = [ComplianceCitation.model_validate(c) for c in citations_raw]
    return ChatTraceRead(
        id=UUID(str(row["id"])),
        session_id=UUID(str(row["session_id"])),
        message_id=UUID(str(row["message_id"])),
        raw_prompt=str(row["raw_prompt"]),
        normalized_query_params=row.get("normalized_query_params") or {},
        intermediate_steps=list(row.get("intermediate_steps") or []),
        final_response=str(row["final_response"]),
        citations=citations,
        created_at=row["created_at"],
    )


async def create_session(advisor_id: UUID, payload: ChatSessionCreate) -> ChatSessionRead:
    client = get_service_supabase()
    result = (
        client.table("compliance_chat_sessions")
        .insert(
            {
                "advisor_id": str(advisor_id),
                "title": payload.title or "Compliance investigation",
            }
        )
        .execute()
    )
    if not result.data:
        raise RuntimeError("failed to create compliance_chat_sessions row")
    return _session_row(result.data[0])


async def list_sessions(advisor_id: UUID, *, limit: int = 50) -> list[ChatSessionRead]:
    client = get_service_supabase()
    result = (
        client.table("compliance_chat_sessions")
        .select("*")
        .eq("advisor_id", str(advisor_id))
        .order("updated_at", desc=True)
        .limit(limit)
        .execute()
    )
    return [_session_row(r) for r in (result.data or [])]


async def get_session_detail(session_id: UUID, advisor_id: UUID) -> ChatSessionDetail | None:
    client = get_service_supabase()
    sess = (
        client.table("compliance_chat_sessions")
        .select("*")
        .eq("id", str(session_id))
        .eq("advisor_id", str(advisor_id))
        .limit(1)
        .execute()
    )
    if not sess.data:
        return None
    messages = (
        client.table("compliance_chat_messages")
        .select("*")
        .eq("session_id", str(session_id))
        .order("created_at")
        .execute()
    )
    traces = (
        client.table("compliance_chat_traces")
        .select("*")
        .eq("session_id", str(session_id))
        .order("created_at")
        .execute()
    )
    return ChatSessionDetail(
        session=_session_row(sess.data[0]),
        messages=[_message_row(m) for m in (messages.data or [])],
        traces=[_trace_row(t) for t in (traces.data or [])],
    )


async def post_chat_message(
    session_id: UUID,
    advisor_id: UUID,
    payload: ChatMessageCreate,
) -> ChatTurnResponse:
    detail = await get_session_detail(session_id, advisor_id)
    if detail is None:
        raise LookupError("session not found")

    client = get_service_supabase()
    safe_content = anonymize_text(payload.content)

    user_ins = (
        client.table("compliance_chat_messages")
        .insert(
            {
                "session_id": str(session_id),
                "role": ChatMessageRole.USER.value,
                "content": safe_content,
            }
        )
        .execute()
    )
    if not user_ins.data:
        raise RuntimeError("failed to insert user message")
    user_msg = _message_row(user_ins.data[0])

    result = await run_compliance_pipeline(safe_content)

    asst_ins = (
        client.table("compliance_chat_messages")
        .insert(
            {
                "session_id": str(session_id),
                "role": ChatMessageRole.ASSISTANT.value,
                "content": result.answer,
            }
        )
        .execute()
    )
    if not asst_ins.data:
        raise RuntimeError("failed to insert assistant message")
    asst_msg = _message_row(asst_ins.data[0])

    citations_json = [c.model_dump(mode="json") for c in result.citations]
    trace_ins = (
        client.table("compliance_chat_traces")
        .insert(
            {
                "session_id": str(session_id),
                "message_id": str(asst_msg.id),
                "raw_prompt": safe_content,
                "normalized_query_params": result.normalized_query_params,
                "intermediate_steps": result.intermediate_steps,
                "final_response": result.answer,
                "citations": citations_json,
            }
        )
        .execute()
    )
    if not trace_ins.data:
        raise RuntimeError("failed to insert compliance_chat_traces row")

    now = datetime.now(timezone.utc).isoformat()
    client.table("compliance_chat_sessions").update({"updated_at": now}).eq(
        "id", str(session_id)
    ).execute()

    logger.info(
        "compliance_chat_turn",
        session_id=str(session_id),
        events=len(result.events),
        citations=len(result.citations),
    )
    return ChatTurnResponse(
        user_message=user_msg,
        assistant_message=asst_msg,
        trace=_trace_row(trace_ins.data[0]),
        confidence_score=result.confidence_score,
    )


def _export_markdown(detail: ChatSessionDetail) -> str:
    lines = [
        f"# Compliance session {detail.session.id}",
        f"Title: {detail.session.title or ''}",
        f"Advisor: {detail.session.advisor_id}",
        "",
        "## Messages",
    ]
    for m in detail.messages:
        lines.append(f"### {m.role.value} ({m.created_at})")
        lines.append(sanitize_for_dom(m.content))
        lines.append("")
    lines.append("## Traces")
    for t in detail.traces:
        lines.append(f"### Trace {t.id}")
        lines.append(f"Prompt: {sanitize_for_dom(t.raw_prompt)}")
        lines.append(f"Params: `{json.dumps(t.normalized_query_params)}`")
        lines.append("Citations:")
        for c in t.citations:
            lines.append(f"- {c.model_dump_json()}")
        lines.append("")
        lines.append(sanitize_for_dom(t.final_response))
        lines.append("")
    return "\n".join(lines)


async def export_session(
    session_id: UUID,
    advisor_id: UUID,
    *,
    include_markdown: bool = True,
) -> TraceExportPayload | None:
    detail = await get_session_detail(session_id, advisor_id)
    if detail is None:
        return None
    return TraceExportPayload(
        session=detail.session,
        messages=detail.messages,
        traces=detail.traces,
        exported_at=datetime.now(timezone.utc),
        format="json",
        markdown=_export_markdown(detail) if include_markdown else None,
    )


async def execute_direct_query(req: ComplianceQueryRequest) -> ComplianceQueryResponse:
    question = req.question or ""
    return await run_compliance_pipeline(question, explicit_filters=req.filters)
