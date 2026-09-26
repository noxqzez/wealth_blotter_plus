"""Deterministic scenario_params field diffs (WF-02 accuracy KPI)."""

from __future__ import annotations

from typing import Any

from backend.app.schemas.scenario import ScenarioParamDiff, ScenarioParams


def diff_scenario_params(
    agent_params: ScenarioParams,
    human_params: ScenarioParams,
) -> tuple[list[ScenarioParamDiff], bool]:
    """
    Compare agent parse vs human-approved params.

    Returns (diffs, is_exact_match). Only non-matching leaf paths are listed;
    ``is_exact_match`` is True when there are zero diffs.
    """
    agent_dump = agent_params.model_dump(mode="json")
    human_dump = human_params.model_dump(mode="json")
    diffs: list[ScenarioParamDiff] = []
    _walk("", agent_dump, human_dump, diffs)
    return diffs, len(diffs) == 0


def _walk(
    path: str,
    agent_val: Any,
    human_val: Any,
    out: list[ScenarioParamDiff],
) -> None:
    if isinstance(agent_val, dict) and isinstance(human_val, dict):
        keys = set(agent_val) | set(human_val)
        for key in sorted(keys):
            child = f"{path}.{key}" if path else str(key)
            _walk(child, agent_val.get(key), human_val.get(key), out)
        return
    if isinstance(agent_val, list) and isinstance(human_val, list):
        if agent_val != human_val:
            out.append(
                ScenarioParamDiff(
                    field_path=path or "$",
                    agent_value=agent_val,
                    human_value=human_val,
                    is_exact_match=False,
                )
            )
        return
    if agent_val != human_val:
        out.append(
            ScenarioParamDiff(
                field_path=path or "$",
                agent_value=agent_val,
                human_value=human_val,
                is_exact_match=False,
            )
        )
