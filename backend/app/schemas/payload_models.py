"""API request / response payloads."""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


class ConfidenceRoute(str, Enum):
    BLOTTER = "blotter"
    HITL = "hitl"


class IngestTickersRequest(BaseModel):
    tickers: list[str] = Field(..., min_length=1)
    market: str | None = Field(default=None, description="us | hk | crypto")


class IngestTickersResponse(BaseModel):
    accepted: int
    advisor_id: UUID
    tickers: list[str]


class AdvisoryAnalyzeRequest(BaseModel):
    portfolio_id: UUID
    # Client identifiers must be opaque IDs — never raw names/account numbers
    client_ref: str = Field(..., min_length=1, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)


class AdvisoryAnalyzeResponse(BaseModel):
    advisor_id: UUID
    portfolio_id: UUID
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    route: ConfidenceRoute
    message: str


class MockOmsSubmitRequest(BaseModel):
    order_ids: list[UUID] = Field(..., min_length=1)


class MockOmsSubmitResponse(BaseModel):
    advisor_id: UUID
    accepted_order_ids: list[UUID]
    simulator_status: str


class AgentAdvisorDiffCreate(BaseModel):
    """Accuracy audit: agent proposal vs advisor-saved outcome."""

    run_id: UUID
    portfolio_id: UUID
    agent_output: dict
    advisor_saved_output: dict
    notes: str | None = None
