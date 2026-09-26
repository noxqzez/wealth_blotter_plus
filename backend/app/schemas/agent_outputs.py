"""Agent output contracts — all production agent results bind here."""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class AssetClass(str, Enum):
    EQUITY_US = "equity_us"
    EQUITY_HK = "equity_hk"
    CRYPTO = "crypto"
    CASH = "cash"
    OTHER = "other"


class AgentBaseOutput(BaseModel):
    """Every agent response must include a calibrated confidence score."""

    confidence_score: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=4000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("confidence_score")
    @classmethod
    def round_confidence(cls, value: float) -> float:
        return round(value, 4)


class ProposedOrder(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32)
    side: str = Field(..., pattern="^(buy|sell)$")
    quantity: float = Field(..., gt=0)
    asset_class: AssetClass = AssetClass.OTHER
    limit_price: float | None = None


class QuantAnalysisOutput(AgentBaseOutput):
    portfolio_id: UUID
    proposed_orders: list[ProposedOrder] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)


class ComplianceCheckOutput(AgentBaseOutput):
    passed: bool
    violations: list[str] = Field(default_factory=list)
    applied_rules_version: int | None = None


class AdvisoryCrewOutput(AgentBaseOutput):
    """Final structured crew result for routing / blotter / HITL."""

    portfolio_id: UUID
    quant: QuantAnalysisOutput | None = None
    compliance: ComplianceCheckOutput | None = None
    anonymized: bool = True


class ResearchAssetClass(str, Enum):
    """Classification labels for research PDF ingest (RAG POC)."""

    US_STOCK = "US_Stock"
    HK_STOCK = "HK_Stock"
    CRYPTO = "Crypto"
    NEITHER = "Neither"


class ResearchSentiment(str, Enum):
    BULLISH = "Bullish"
    BEARISH = "Bearish"
    NEUTRAL = "Neutral"


class DocumentClassificationOutput(AgentBaseOutput):
    """Structured output from the document classification agent."""

    asset_class: ResearchAssetClass
    ticker: str | None = Field(
        default=None,
        max_length=32,
        description="Ticker e.g. AAPL, 0700.HK, BTC. Null when Neither or unclear.",
    )
    source_firm: str | None = Field(default=None, max_length=128)
    overall_sentiment: ResearchSentiment = ResearchSentiment.NEUTRAL
