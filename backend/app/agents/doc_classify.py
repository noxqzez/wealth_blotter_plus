"""CrewAI classification for uploaded research PDFs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
import yaml
from crewai import Agent, Crew, Process, Task

from backend.app.agents.llm import build_llm
from backend.app.agents.self_healing import run_with_self_healing
from backend.app.config import get_settings
from backend.app.schemas.agent_outputs import (
    DocumentClassificationOutput,
    ResearchAssetClass,
    ResearchSentiment,
)

logger = structlog.get_logger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent / "config"


def _load_yaml(name: str) -> dict[str, Any]:
    path = _CONFIG_DIR / name
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {name}")
    return data


async def classify_document_excerpt(excerpt: str) -> DocumentClassificationOutput:
    """
    Classify a document excerpt into US_Stock / HK_Stock / Crypto / Neither.

    Excerpt must already be anonymized / free of client PII before calling.
    """
    settings = get_settings()
    max_chars = settings.rag_classify_excerpt_chars
    safe_excerpt = excerpt[:max_chars]

    agents_cfg = _load_yaml("doc_classify_agents.yaml")
    tasks_cfg = _load_yaml("doc_classify_tasks.yaml")

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
            allow_delegation=False,
            verbose=True,
        )

    classifier = agents.get("document_classifier")
    if classifier is None:
        raise KeyError("document_classifier missing from doc_classify_agents.yaml")

    task_raw = tasks_cfg.get("classify_document") or {}
    description = str(task_raw.get("description", "")).strip()
    expected = str(task_raw.get("expected_output", "DocumentClassificationOutput"))
    description = (
        f"{description}\n\n"
        f"Document excerpt (first pages only):\n{safe_excerpt}\n\n"
        f"Return ONLY valid JSON matching `{expected}` "
        f"with confidence_score in [0.0, 1.0]."
    )

    task = Task(
        name="classify_document",
        description=description,
        expected_output=f"Valid JSON for {expected}",
        agent=classifier,
    )
    crew = Crew(
        agents=[classifier],
        tasks=[task],
        process=Process.sequential,
        verbose=True,
    )

    schema_hint = json.dumps(
        {
            "asset_class": "US_Stock | HK_Stock | Crypto | Neither",
            "ticker": "AAPL | 0700.HK | BTC | null",
            "source_firm": "string or null",
            "overall_sentiment": "Bullish | Bearish | Neutral",
            "confidence_score": 0.0,
            "rationale": "short string",
        }
    )

    async def _caller(prompt: str) -> str:
        if prompt != "__initial__":
            heal_llm = build_llm()
            heal_agent = Agent(
                role="Schema Repair Agent",
                goal="Emit valid DocumentClassificationOutput JSON only.",
                backstory="Corrects malformed classification JSON.",
                llm=heal_llm,
                allow_delegation=False,
                verbose=False,
            )
            heal_task = Task(
                name="schema_repair",
                description=f"{prompt}\n\nRequired shape example:\n{schema_hint}",
                expected_output="Valid DocumentClassificationOutput JSON only",
                agent=heal_agent,
            )
            heal_crew = Crew(
                agents=[heal_agent],
                tasks=[heal_task],
                process=Process.sequential,
                verbose=False,
            )
            result = await heal_crew.kickoff_async()
            return str(result)
        result = await crew.kickoff_async()
        return str(result)

    try:
        output = await run_with_self_healing(
            schema=DocumentClassificationOutput,
            prompt="__initial__",
            caller=_caller,
            max_retries=3,
        )
        logger.info(
            "document_classified",
            asset_class=output.asset_class.value,
            ticker=output.ticker,
            confidence=output.confidence_score,
        )
        return output
    except Exception as exc:  # noqa: BLE001
        logger.warning("document_classification_fallback", error=str(exc))
        return DocumentClassificationOutput(
            asset_class=ResearchAssetClass.NEITHER,
            ticker=None,
            source_firm=None,
            overall_sentiment=ResearchSentiment.NEUTRAL,
            confidence_score=0.35,
            rationale=(
                "Classification failed or invalid JSON; quarantined. "
                f"({type(exc).__name__})"
            ),
        )
