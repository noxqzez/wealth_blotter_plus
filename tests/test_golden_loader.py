"""Smoke tests for the golden-case loader."""

from __future__ import annotations

import pytest

from tests.golden.loader import load_golden


@pytest.mark.parametrize(
    "suite",
    [
        "import_validation",
        "compliance_guardrails",
        "agent_drift",
        "agent_routing",
    ],
)
def test_golden_suites_load_and_have_ids(suite: str) -> None:
    cases = load_golden(suite)
    assert cases
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), f"duplicate ids in {suite}: {ids}"
    for case in cases:
        assert "expect" in case
        assert "input" in case
