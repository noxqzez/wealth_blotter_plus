"""CrewAI orchestrator scaffold — wire roles/tasks when product defines them."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
import yaml

from backend.app.config import get_settings
from backend.app.schemas.agent_outputs import AdvisoryCrewOutput

logger = structlog.get_logger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent / "config"


def load_yaml_config(name: str) -> dict[str, Any]:
    path = _CONFIG_DIR / name
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {name}")
    return data


def build_llm_config() -> dict[str, Any]:
    """Shared LLM settings — low temperature by default; YAML may override per agent."""
    settings = get_settings()
    return {
        "provider": settings.llm_provider,
        "model": settings.ollama_model,
        "base_url": settings.ollama_base_url,
        "temperature": settings.llm_temperature,
        "use_cloud": settings.use_cloud_llm,
    }


async def run_advisory_crew(
    *,
    portfolio_id: UUID,
    anonymized_context: dict[str, Any],
) -> AdvisoryCrewOutput:
    """
    Placeholder crew execution.

    When roles are finalized, instantiate CrewAI Agents/Tasks from agents.yaml /
    tasks.yaml, run with self_healing.run_with_self_healing, then confidence-route.
    """
    agents_cfg = load_yaml_config("agents.yaml")
    tasks_cfg = load_yaml_config("tasks.yaml")
    llm_cfg = build_llm_config()

    logger.info(
        "advisory_crew_stub",
        portfolio_id=str(portfolio_id),
        agent_keys=list(agents_cfg.keys()),
        task_keys=list(tasks_cfg.keys()),
        llm=llm_cfg,
        context_keys=list(anonymized_context.keys()),
    )

    # Deterministic stub below HITL threshold until real crew is wired
    return AdvisoryCrewOutput(
        portfolio_id=portfolio_id,
        confidence_score=0.5,
        rationale="Scaffold stub — replace with CrewAI execution",
        anonymized=True,
    )
