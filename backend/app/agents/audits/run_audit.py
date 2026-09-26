"""Write sequenced per-agent outputs to workflow-labeled audit files."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger(__name__)

_AUDITS_ROOT = Path(__file__).resolve().parent

# Workflow labels — one folder per crew/process for manual audit trails.
WORKFLOW_PORTFOLIO_SUGGESTIONS = "portfolio_suggestions"


def write_workflow_audit(
    *,
    workflow: str,
    portfolio_id: UUID,
    steps: list[dict[str, Any]],
    final_output: str | None = None,
    llm_config: dict[str, Any] | None = None,
    status: str = "ok",
    error: str | None = None,
) -> Path:
    """
    Persist one markdown audit file listing each agent output in run order.

    Files land under ``audits/<workflow>/local_audit_YYYYMMDDHHMM.md``.
    """
    workflow_dir = _AUDITS_ROOT / workflow
    workflow_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d%H%M")
    path = workflow_dir / f"local_audit_{stamp}.md"
    if path.exists():
        # Same-minute collision: append seconds so we never overwrite.
        stamp_s = datetime.now().strftime("%Y%m%d%H%M%S")
        path = workflow_dir / f"local_audit_{stamp_s}.md"

    lines: list[str] = [
        f"# Workflow audit: `{workflow}`",
        "",
        f"- **status:** {status}",
        f"- **portfolio_id:** `{portfolio_id}`",
        f"- **local_time:** {stamp}",
        f"- **steps:** {len(steps)}",
    ]
    if llm_config:
        lines.append(f"- **llm:** `{llm_config}`")
    if error:
        lines.extend(["", "## Error", "", "```", error, "```"])

    lines.extend(["", "## Agent outputs (sequence)", ""])

    for index, step in enumerate(steps, start=1):
        agent = step.get("agent") or "unknown_agent"
        role = step.get("role") or ""
        task_key = step.get("task_key") or step.get("name") or f"step_{index}"
        expected = step.get("expected_output") or ""
        role_suffix = f" — {role}" if role else ""
        lines.append(f"### {index}. `{task_key}` · agent `{agent}`{role_suffix}")
        if expected:
            lines.append(f"- **expected_output:** `{expected}`")
        lines.append("")
        lines.append("```text")
        lines.append(str(step.get("raw") or "").rstrip() or "(empty)")
        lines.append("```")
        lines.append("")

    if final_output is not None:
        lines.extend(
            [
                "## Validated / final crew output",
                "",
                "```text",
                final_output.rstrip() or "(empty)",
                "```",
                "",
            ]
        )

    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(
        "workflow_audit_written",
        workflow=workflow,
        path=str(path),
        steps=len(steps),
        status=status,
    )
    return path


def steps_from_crew_result(
    result: Any,
    *,
    task_keys: list[str] | None = None,
    agent_roles: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Map CrewAI ``CrewOutput.tasks_output`` into ordered audit step dicts."""
    tasks_output = getattr(result, "tasks_output", None) or []
    roles = agent_roles or {}
    steps: list[dict[str, Any]] = []

    for index, task_out in enumerate(tasks_output):
        name = getattr(task_out, "name", None)
        task_key = name or (
            task_keys[index] if task_keys and index < len(task_keys) else f"task_{index + 1}"
        )
        agent_name = str(getattr(task_out, "agent", "") or "")
        steps.append(
            {
                "task_key": task_key,
                "name": name,
                "agent": agent_name,
                "role": roles.get(agent_name, ""),
                "expected_output": getattr(task_out, "expected_output", None),
                "raw": getattr(task_out, "raw", None) or str(task_out),
            }
        )
    return steps
