"""CrewAI orchestrator — local Ollama by default."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
import yaml
from crewai import Agent, Crew, Process, Task

from backend.app.agents.audits import (
    WORKFLOW_PORTFOLIO_SUGGESTIONS,
    write_workflow_audit,
)
from backend.app.agents.audits.run_audit import steps_from_crew_result
from backend.app.agents.llm import build_llm
from backend.app.agents.self_healing import run_with_self_healing
from backend.app.config import get_settings
from backend.app.schemas.agent_outputs import AdvisoryCrewOutput

logger = structlog.get_logger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent / "config"

# Manual audit label for this crew (see audits/portfolio_suggestions/).
_WORKFLOW = WORKFLOW_PORTFOLIO_SUGGESTIONS


def load_yaml_config(name: str) -> dict[str, Any]:
    path = _CONFIG_DIR / name
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {name}")
    return data


def build_llm_config() -> dict[str, Any]:
    """Shared LLM settings snapshot for logging / diagnostics."""
    settings = get_settings()
    return {
        "provider": settings.llm_provider,
        "model": settings.ollama_model,
        "base_url": settings.ollama_base_url,
        "temperature": settings.llm_temperature,
        "use_cloud": settings.use_cloud_llm,
    }


def _build_agents(agents_cfg: dict[str, Any]) -> dict[str, Agent]:
    agents: dict[str, Agent] = {}
    for key, raw in agents_cfg.items():
        if not isinstance(raw, dict):
            continue
        temp = raw.get("temperature")
        llm = build_llm(temperature=float(temp) if temp is not None else None)
        agents[key] = Agent(
            role=str(raw.get("role", key)),
            goal=str(raw.get("goal", "")),
            backstory=str(raw.get("backstory", "")),
            llm=llm,
            allow_delegation=bool(raw.get("allow_delegation", False)),
            verbose=True,
        )
    return agents


def _build_tasks(
    tasks_cfg: dict[str, Any],
    agents: dict[str, Agent],
    *,
    portfolio_id: UUID,
    anonymized_context: dict[str, Any],
) -> list[Task]:
    context_blob = json.dumps(
        {"portfolio_id": str(portfolio_id), "context": anonymized_context},
        default=str,
    )
    built: dict[str, Task] = {}
    ordered: list[Task] = []

    for key, raw in tasks_cfg.items():
        if not isinstance(raw, dict):
            continue
        agent_key = str(raw.get("agent", ""))
        agent = agents.get(agent_key)
        if agent is None:
            raise KeyError(f"Task {key} references unknown agent {agent_key}")

        description = str(raw.get("description", "")).strip()
        expected = str(raw.get("expected_output", "JSON object"))
        description = (
            f"{description}\n\n"
            f"Input context (anonymized JSON):\n{context_blob}\n\n"
            f"Return ONLY valid JSON matching schema `{expected}` "
            f"including confidence_score in [0.0, 1.0]."
        )

        ctx_keys = raw.get("context") or []
        context_tasks = [built[c] for c in ctx_keys if c in built]

        task = Task(
            name=key,
            description=description,
            expected_output=f"Valid JSON for {expected}",
            agent=agent,
            context=context_tasks or None,
            async_execution=bool(raw.get("async_execution", False)),
        )
        built[key] = task
        ordered.append(task)

    return ordered


def _role_by_agent_key(agents_cfg: dict[str, Any]) -> dict[str, str]:
    """Map Agent.role display string → YAML agent key for audit labels."""
    mapping: dict[str, str] = {}
    for key, raw in agents_cfg.items():
        if isinstance(raw, dict):
            mapping[str(raw.get("role", key))] = key
    return mapping


async def run_advisory_crew(
    *,
    portfolio_id: UUID,
    anonymized_context: dict[str, Any],
) -> AdvisoryCrewOutput:
    """
    Run the configured CrewAI crew against Ollama (or cloud LLM if enabled).

    Final crew text is validated into AdvisoryCrewOutput with self-heal retries.
    Each run also writes a sequenced audit file under audits/portfolio_suggestions/.
    """
    agents_cfg = load_yaml_config("agents.yaml")
    tasks_cfg = load_yaml_config("tasks.yaml")
    llm_cfg = build_llm_config()
    task_keys = [k for k, v in tasks_cfg.items() if isinstance(v, dict)]
    role_to_key = _role_by_agent_key(agents_cfg)
    audit_steps: list[dict[str, Any]] = []

    logger.info(
        "advisory_crew_start",
        portfolio_id=str(portfolio_id),
        workflow=_WORKFLOW,
        agent_keys=list(agents_cfg.keys()),
        task_keys=task_keys,
        llm=llm_cfg,
        context_keys=list(anonymized_context.keys()),
    )

    agents = _build_agents(agents_cfg)
    tasks = _build_tasks(
        tasks_cfg,
        agents,
        portfolio_id=portfolio_id,
        anonymized_context=anonymized_context,
    )

    crew = Crew(
        agents=list(agents.values()),
        tasks=tasks,
        process=Process.sequential,
        verbose=True,
    )

    def _record_crew_steps(result: Any, *, heal_attempt: int | None = None) -> None:
        for step in steps_from_crew_result(
            result,
            task_keys=task_keys if heal_attempt is None else None,
        ):
            display_role = step.get("agent") or ""
            agent_key = role_to_key.get(display_role, display_role)
            step["agent"] = agent_key
            step["role"] = display_role
            if heal_attempt is not None:
                step["task_key"] = f"schema_repair_attempt_{heal_attempt}"
            audit_steps.append(step)

    async def _caller(prompt: str) -> str:
        # Feed correction prompt into a one-shot staging-style kickoff when healing
        if prompt != "__initial__":
            heal_attempt = sum(
                1 for s in audit_steps if str(s.get("task_key", "")).startswith("schema_repair")
            ) + 1
            heal_llm = build_llm()
            heal_agent = Agent(
                role="Schema Repair Agent",
                goal="Emit valid AdvisoryCrewOutput JSON only.",
                backstory="Corrects malformed crew JSON to match Pydantic schemas.",
                llm=heal_llm,
                allow_delegation=False,
                verbose=False,
            )
            heal_task = Task(
                name=f"schema_repair_attempt_{heal_attempt}",
                description=prompt,
                expected_output="Valid AdvisoryCrewOutput JSON only",
                agent=heal_agent,
            )
            heal_crew = Crew(
                agents=[heal_agent],
                tasks=[heal_task],
                process=Process.sequential,
                verbose=False,
            )
            result = await heal_crew.kickoff_async()
            _record_crew_steps(result, heal_attempt=heal_attempt)
            return str(result)

        result = await crew.kickoff_async()
        _record_crew_steps(result)
        return str(result)

    try:
        output = await run_with_self_healing(
            schema=AdvisoryCrewOutput,
            prompt="__initial__",
            caller=_caller,
            max_retries=3,
        )
        # Ensure portfolio_id is set even if model omitted it
        if output.portfolio_id != portfolio_id:
            output = output.model_copy(update={"portfolio_id": portfolio_id})
        write_workflow_audit(
            workflow=_WORKFLOW,
            portfolio_id=portfolio_id,
            steps=audit_steps,
            final_output=output.model_dump_json(indent=2),
            llm_config=llm_cfg,
            status="ok",
        )
        logger.info(
            "advisory_crew_complete",
            portfolio_id=str(portfolio_id),
            confidence=output.confidence_score,
            workflow=_WORKFLOW,
        )
        return output
    except Exception as exc:  # noqa: BLE001
        logger.warning("advisory_crew_failed_fallback", error=str(exc))
        fallback = AdvisoryCrewOutput(
            portfolio_id=portfolio_id,
            confidence_score=0.4,
            rationale=(
                "Crew execution failed or invalid JSON; routed to HITL. "
                f"({type(exc).__name__})"
            ),
            anonymized=True,
        )
        write_workflow_audit(
            workflow=_WORKFLOW,
            portfolio_id=portfolio_id,
            steps=audit_steps,
            final_output=fallback.model_dump_json(indent=2),
            llm_config=llm_cfg,
            status="fallback",
            error=str(exc),
        )
        return fallback
