"""Minimal CrewAI + Ollama demo: one agent prints Hello world.

Also writes a sequenced audit file under audits/portfolio_suggestions/
so you can verify the manual-audit path without CrewAI Desktop.

Run:
  .venv/bin/python -m backend.app.agents.demo_hello
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from crewai import Agent, Crew, Process, Task

from backend.app.agents.audits import (
    WORKFLOW_PORTFOLIO_SUGGESTIONS,
    write_workflow_audit,
)
from backend.app.agents.audits.run_audit import steps_from_crew_result
from backend.app.agents.crew import build_llm_config
from backend.app.agents.llm import build_llm


_CONFIG_DIR = Path(__file__).resolve().parent / "config"


def _load_yaml(name: str) -> dict[str, Any]:
    with (_CONFIG_DIR / name).open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {name}")
    return data


def run_hello_crew() -> tuple[str, Path]:
    """Build a one-agent crew from demo YAML, kick off, and write an audit file."""
    agents_cfg = _load_yaml("demo_hello_agents.yaml")
    tasks_cfg = _load_yaml("demo_hello_tasks.yaml")
    llm_cfg = build_llm_config()
    run_id = uuid4()

    raw = agents_cfg["hello_writer"]
    llm = build_llm(temperature=float(raw.get("temperature", 0.2)))

    greeter = Agent(
        role=str(raw["role"]),
        goal=str(raw["goal"]),
        backstory=str(raw["backstory"]),
        llm=llm,
        allow_delegation=bool(raw.get("allow_delegation", False)),
        verbose=True,
    )

    task_raw = tasks_cfg["say_hello"]
    task = Task(
        name="say_hello",
        description=str(task_raw["description"]).strip(),
        expected_output=str(task_raw["expected_output"]),
        agent=greeter,
    )

    crew = Crew(
        agents=[greeter],
        tasks=[task],
        process=Process.sequential,
        verbose=True,
    )
    result = crew.kickoff()
    output = str(result)

    steps = steps_from_crew_result(result, task_keys=["say_hello"])
    for step in steps:
        step["agent"] = "hello_writer"
        step["role"] = str(raw["role"])

    audit_path = write_workflow_audit(
        workflow=WORKFLOW_PORTFOLIO_SUGGESTIONS,
        portfolio_id=run_id,
        steps=steps,
        final_output=output,
        llm_config=llm_cfg,
        status="ok",
    )
    return output, audit_path


def main() -> None:
    print("=== Demo CrewAI agent (Ollama) ===")
    print("Persona: Friendly Greeter (hello_writer)")
    print("Task: Hello world + who the agent is")
    print(f"Audit workflow: {WORKFLOW_PORTFOLIO_SUGGESTIONS}")
    print("---")
    output, audit_path = run_hello_crew()
    print("---")
    print("Crew output:")
    print(output)
    print("---")
    print(f"Audit file: {audit_path}")


if __name__ == "__main__":
    main()
