"""Portfolio & trade analysis endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.app.agents.crew import run_advisory_crew
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
    Trigger CrewAI analysis via local Ollama (or cloud LLM if USE_CLOUD_LLM=true).

    Confidence routing: >= threshold → blotter; else HITL.
    """
    result = await run_advisory_crew(
        portfolio_id=payload.portfolio_id,
        anonymized_context={
            "client_ref": payload.client_ref,
            "notes": payload.notes,
        },
    )
    route = _route_for_confidence(result.confidence_score, settings.confidence_threshold)
    return AdvisoryAnalyzeResponse(
        advisor_id=user.advisor_id,
        portfolio_id=payload.portfolio_id,
        confidence_score=result.confidence_score,
        route=route,
        message=result.rationale or f"Crew complete; routed to {route.value}",
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
