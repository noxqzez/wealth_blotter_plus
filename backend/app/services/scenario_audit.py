"""WF-02 per-step message-bus persistence into ``audit_events``.

Trace key: ``session_id = scenario_run_id`` (also in ``entity_refs`` /
``normalized_params``). One row per hop so opti_folio lineage is queryable.

Phase-1 still emits house/market research + quant rows (stub or deterministic)
so the hop sequence is complete even before Phase-2 LLM research is wired.
"""

from __future__ import annotations

from typing import Any, Mapping
from uuid import UUID

import structlog

from backend.app.schemas.compliance import AuditSourceType
from backend.app.services.audit_events import insert_audit_event_row

logger = structlog.get_logger(__name__)

SOURCE_SCENARIO_WF02 = "scenario_wf02"

# Ordered hop names (normalized_params.step_order)
STEP_PARSE = "scenario_agent_parse"
STEP_PARAMS_UPDATE = "human_params_update"
STEP_MARKET_HISTORY = "market_history"
STEP_HOUSE_RESEARCH = "house_research_agent"
STEP_MARKET_RESEARCH = "market_research_agent"
STEP_STRATEGIST = "strategist_agent"
STEP_RISK_BASE = "risk_engine_base"
STEP_QUANT = "quant_agent_optimizer"
STEP_QUANT_SAFETY = "quant_safety_gate"
STEP_COMPLIANCE = "compliance_resolve"
STEP_RISK_OPTI = "risk_engine_opti"
STEP_PAST_ANALOGUE = "past_analogue_agent"
STEP_CONTENT = "content_agent"
STEP_RUN_COMPLETE = "scenario_run_complete"
STEP_RUN_FAILED = "scenario_run_failed"

STEP_ORDER: dict[str, int] = {
    STEP_PARSE: 10,
    STEP_PARAMS_UPDATE: 20,
    STEP_MARKET_HISTORY: 30,
    STEP_HOUSE_RESEARCH: 40,
    STEP_MARKET_RESEARCH: 50,
    STEP_STRATEGIST: 60,
    STEP_RISK_BASE: 70,
    STEP_QUANT_SAFETY: 75,
    STEP_QUANT: 80,
    STEP_COMPLIANCE: 90,
    STEP_RISK_OPTI: 100,
    STEP_PAST_ANALOGUE: 105,
    STEP_CONTENT: 110,
    STEP_RUN_COMPLETE: 120,
    STEP_RUN_FAILED: 120,
}


def write_scenario_step_audit(
    *,
    scenario_run_id: UUID,
    portfolio_id: UUID,
    step: str,
    agent_or_component: str,
    input_payload: Mapping[str, Any] | None = None,
    output_payload: Mapping[str, Any] | None = None,
    messages: list[str] | None = None,
    advisor_id: UUID | None = None,
    client_alias: str | None = None,
    phase: str = "phase1",
    implementation: str = "deterministic",
    extra_entity_refs: Mapping[str, Any] | None = None,
    ok: bool = True,
) -> str | None:
    """
    Insert one ``agent_telemetry`` (or application) audit row for a scenario hop.

    Returns audit_events.id as str, or None if insert failed (never raises —
    audit must not break the run).
    """
    order = STEP_ORDER.get(step, 0)
    source_type = (
        AuditSourceType.APPLICATION_EVENT.value
        if step in {STEP_PARAMS_UPDATE, STEP_RUN_COMPLETE, STEP_RUN_FAILED}
        else AuditSourceType.AGENT_TELEMETRY.value
    )
    entity_refs: dict[str, Any] = {
        "scenario_run_id": str(scenario_run_id),
        "portfolio_id": str(portfolio_id),
        "workflow_id": str(scenario_run_id),
        "step": step,
    }
    if client_alias:
        entity_refs["client_alias"] = client_alias
    if extra_entity_refs:
        entity_refs.update(dict(extra_entity_refs))

    payload: dict[str, Any] = {
        "step": step,
        "step_order": order,
        "agent_or_component": agent_or_component,
        "implementation": implementation,
        "phase": phase,
        "ok": ok,
        "input": dict(input_payload or {}),
        "output": dict(output_payload or {}),
        "messages": list(messages or []),
    }
    normalized: dict[str, Any] = {
        "event_action": f"scenario_step_{step}",
        "workflow": "wf02_scenario",
        "scenario_run_id": str(scenario_run_id),
        "portfolio_id": str(portfolio_id),
        "step": step,
        "step_order": order,
        "agent_or_component": agent_or_component,
    }

    try:
        row = insert_audit_event_row(
            source_type=source_type,
            source_name=SOURCE_SCENARIO_WF02,
            event_action=f"scenario_step_{step}",
            event_type="scenario_workflow",
            advisor_id=advisor_id,
            actor_id=advisor_id,
            session_id=str(scenario_run_id),
            entity_refs=entity_refs,
            payload=payload,
            normalized_params=normalized,
        )
        row_id = str(row["id"]) if row.get("id") else None
        logger.info(
            "scenario_step_audit_written",
            scenario_run_id=str(scenario_run_id),
            step=step,
            audit_event_id=row_id,
        )
        return row_id
    except Exception as exc:  # noqa: BLE001 — audit is best-effort
        logger.warning(
            "scenario_step_audit_failed",
            scenario_run_id=str(scenario_run_id),
            step=step,
            error=str(exc),
        )
        return None
