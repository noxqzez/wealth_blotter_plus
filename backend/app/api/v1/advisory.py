"""Portfolio & trade analysis endpoints (scaffold)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.app.config import Settings, get_settings
from backend.app.core.security import AuthContext, get_current_user
from backend.app.schemas.payload_models import (
    AdvisoryAnalyzeRequest,
    AdvisoryAnalyzeResponse,
    ConfidenceRoute,
    MockOmsSubmitRequest,
    MockOmsSubmitResponse,
)

router = APIRouter()


def _route_for_confidence(score: float, threshold: float) -> ConfidenceRoute:
    if score >= threshold:
        return ConfidenceRoute.BLOTTER
    return ConfidenceRoute.HITL


@router.post("/analyze", response_model=AdvisoryAnalyzeResponse)
async def analyze_portfolio(
    payload: AdvisoryAnalyzeRequest,
    user: AuthContext = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> AdvisoryAnalyzeResponse:
    """
    Trigger CrewAI analysis (stub).

    Real orchestration wires `backend.app.agents.crew` + self-healing.
    Confidence routing: >= threshold → blotter; else HITL.
    """
    # Placeholder confidence until crew is wired
    confidence = 0.5
    route = _route_for_confidence(confidence, settings.confidence_threshold)
    return AdvisoryAnalyzeResponse(
        advisor_id=user.advisor_id,
        portfolio_id=payload.portfolio_id,
        confidence_score=confidence,
        route=route,
        message=(
            "Scaffold response — wire CrewAI in agents/crew.py. "
            "HITL expected until real agent confidence is produced."
        ),
    )


@router.post("/oms/simulate", response_model=MockOmsSubmitResponse)
async def simulate_oms_submit(
    payload: MockOmsSubmitRequest,
    user: AuthContext = Depends(get_current_user),
) -> MockOmsSubmitResponse:
    """Dummy OMS simulator — accepts blotter order ids and returns mock ack."""
    return MockOmsSubmitResponse(
        advisor_id=user.advisor_id,
        accepted_order_ids=payload.order_ids,
        simulator_status="ACK_SIMULATED",
    )
