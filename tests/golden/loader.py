"""Load declarative golden cases from ``tests/golden/*.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

GOLDEN_DIR = Path(__file__).resolve().parent


def load_golden(name: str) -> list[dict[str, Any]]:
    """
    Load a golden suite file.

    ``name`` may be ``import_validation``, ``import_validation.yaml``,
    or a path relative to ``tests/golden/``.
    """
    path = _resolve(name)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TypeError(f"Golden file {path} must be a YAML list of cases")
    cases: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise TypeError(f"{path} case[{i}] must be a mapping")
        if "id" not in item:
            raise ValueError(f"{path} case[{i}] missing required 'id'")
        if "expect" not in item:
            raise ValueError(f"{path} case[{i}] ({item['id']}) missing required 'expect'")
        cases.append(item)
    return cases


def case_ids(cases: list[dict[str, Any]]) -> list[str]:
    return [str(c["id"]) for c in cases]


def _resolve(name: str) -> Path:
    candidate = Path(name)
    if candidate.is_file():
        return candidate
    stem = name if name.endswith((".yaml", ".yml")) else f"{name}.yaml"
    path = GOLDEN_DIR / stem
    if not path.is_file():
        raise FileNotFoundError(f"Golden suite not found: {path}")
    return path
