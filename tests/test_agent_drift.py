"""Agent routing / drift tests driven by ``tests/golden/agent_drift.yaml``."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.api.v1.advisory import _route_for_confidence
from backend.app.schemas.agent_outputs import AdvisoryCrewOutput
from backend.app.schemas.payload_models import ConfidenceRoute
from tests.golden.loader import case_ids, load_golden

_CASES = load_golden("agent_drift")
_UNIT = [c for c in _CASES if not c.get("live_llm")]
_LIVE = [c for c in _CASES if c.get("live_llm")]


@pytest.mark.parametrize("case", _UNIT, ids=case_ids(_UNIT))
def test_golden_agent_drift_unit(
    case: dict,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expect = case["expect"]
    inp = case.get("input") or {}
    threshold = float(inp.get("threshold") or expect.get("threshold") or 0.85)

    # Pure routing table (no HTTP).
    if "score" in inp and "notes" not in inp:
        route = _route_for_confidence(float(inp["score"]), threshold)
        assert route.value == str(expect["route"])
        return

    pid = uuid4()
    mock_conf = float(inp["mock_confidence"])
    monkeypatch.setattr(
        "backend.app.api.v1.advisory.run_advisory_crew",
        AsyncMock(
            return_value=AdvisoryCrewOutput(
                portfolio_id=pid,
                confidence_score=mock_conf,
                rationale=f"golden:{case['id']}",
            )
        ),
    )
    resp = api_client.post(
        "/api/v1/advisory/analyze",
        json={
            "portfolio_id": str(pid),
            "client_ref": str(inp["client_ref"]),
            "notes": str(inp.get("notes") or ""),
        },
    )
    if "http_status" in expect:
        assert resp.status_code == int(expect["http_status"])
    body = resp.json()
    if "route" in expect:
        assert body["route"] == str(expect["route"])
        assert body["route"] in {ConfidenceRoute.BLOTTER.value, ConfidenceRoute.HITL.value}
    if "confidence_min" in expect:
        assert float(body["confidence_score"]) >= float(expect["confidence_min"])
    if "confidence_max" in expect:
        assert float(body["confidence_score"]) <= float(expect["confidence_max"])


@pytest.mark.live_llm
@pytest.mark.asyncio
@pytest.mark.parametrize("case", _LIVE, ids=case_ids(_LIVE) or ["no_live_cases"])
async def test_golden_agent_drift_live(case: dict) -> None:
    """Runs only when ``WBP_LIVE_LLM=1`` (see conftest)."""
    from backend.app.agents.crew import run_advisory_crew

    expect = case["expect"]
    inp = case.get("input") or {}
    result = await run_advisory_crew(
        portfolio_id=uuid4(),
        anonymized_context={
            "client_ref": str(inp["client_ref"]),
            "notes": str(inp.get("notes") or ""),
        },
    )
    score = float(result.confidence_score)
    if "confidence_min" in expect:
        assert score >= float(expect["confidence_min"])
    if "confidence_max" in expect:
        assert score <= float(expect["confidence_max"])
    if expect.get("rationale_not_empty"):
        assert isinstance(result.rationale, str) and result.rationale.strip()
    if "route" in expect:
        threshold = float(inp.get("threshold") or expect.get("threshold") or 0.85)
        assert _route_for_confidence(score, threshold).value == str(expect["route"])
