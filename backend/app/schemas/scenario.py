"""Pydantic contracts for market scenario workflow (WF-02).

Phase 1 UAT: parse → human approve/edit params → run (internal research∥risk →
optimize → stage → content) → PDF. Phase 2 adds richer market research / cloud LLM
without breaking these shapes (optional fields + status values reserved).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


ScenarioStatusLiteral = Literal[
    "PARSED",
    "PARAMS_UPDATED",
    "RUNNING",
    "COMPLETED",
    "FAILED",
]

SeverityLiteral = Literal["low", "medium", "high"]
ToneLiteral = Literal["risk_off", "risk_on", "neutral"]
AssetClassLiteral = Literal["EQUITY", "CASH", "CRYPTO"]
TradeSideLiteral = Literal["BUY", "SELL"]

# Extensible: Phase 1 uses stub|house_rag; Phase 2 adds market_web|full
StrategyViewSourceLiteral = Literal["stub", "house_rag", "market_web", "full"]
CoinTemperatureLiteral = Literal[1, 2, 3, 4, 5]  # 3 = neutral; default 2


# ---------------------------------------------------------------------------
# scenario_params (agent parse + human-approved)
# ---------------------------------------------------------------------------


class ExplicitMarketLevels(BaseModel):
    model_config = {"extra": "allow"}

    brent_oil_target: float | None = None


class DetectedFactors(BaseModel):
    macro_factors: list[str] = Field(default_factory=list)
    explicit_market_levels: ExplicitMarketLevels = Field(
        default_factory=ExplicitMarketLevels
    )
    impacted_asset_classes: list[str] = Field(default_factory=list)


class MarketDataRequestParams(BaseModel):
    lookback_window: str = "5Y"
    frequency: str = "daily"
    required_fields: list[str] = Field(
        default_factory=lambda: ["adjusted_close", "dividend_yield", "volume"]
    )
    include_cash_proxy: bool = True


class DownstreamInstructions(BaseModel):
    for_market_data_service: str = ""
    for_research_agents: str = ""
    for_quant_agent: str = ""


class ScenarioConfidence(BaseModel):
    scenario_classification_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    horizon_selection_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ScenarioBlock(BaseModel):
    scenario_name: str
    scenario_type: str
    severity: SeverityLiteral = "medium"
    tone: ToneLiteral = "neutral"
    base_currency: Literal["USD"] = "USD"
    investment_horizon: str = "1M"
    lookback_window: str = "5Y"
    data_frequency: str = "daily"


class ScenarioParams(BaseModel):
    """Structured params for research / risk / optimizer (agent or human-edited)."""

    message_type: Literal["scenario_params"] = "scenario_params"
    workflow_id: str | None = None
    source_agent: str = "scenario_agent"
    user_prompt: str
    scenario: ScenarioBlock
    detected_factors: DetectedFactors = Field(default_factory=DetectedFactors)
    market_data_request_params: MarketDataRequestParams = Field(
        default_factory=MarketDataRequestParams
    )
    research_context_tags: list[str] = Field(default_factory=list)
    downstream_instructions: DownstreamInstructions = Field(
        default_factory=DownstreamInstructions
    )
    confidence: ScenarioConfidence = Field(default_factory=ScenarioConfidence)
    coin_temperature: int | None = Field(
        default=None,
        ge=1,
        le=5,
        description=(
            "Optional per-run crypto dial override (1–5, 3=neutral). "
            "Firm default lives in configs.business_guardrails for the "
            "management Streamlit dashboard — not advisor Excel."
        ),
    )
    risk_aversion: float | None = Field(
        default=None,
        gt=0.0,
        description="Mean-variance λ; None → optimizer default (2.5).",
    )
    freeze_list: list[str] = Field(
        default_factory=list,
        description="Tickers locked to current BASE folio weight.",
    )
    weight_overrides: dict[str, float] = Field(
        default_factory=dict,
        description="Target weights as fractions in [0, 1] (Excel uses percent form).",
    )


# ---------------------------------------------------------------------------
# Research / strategy (Phase 1 may stub; shapes stay stable for Phase 2)
# ---------------------------------------------------------------------------


class ResearchSourceRef(BaseModel):
    document_id: str | None = None
    section: str | None = None
    retrieval_score: float | None = None
    url: str | None = None
    publisher: str | None = None
    retrieved_at: datetime | None = None


class AssetResearchView(BaseModel):
    asset: str
    asset_class: str
    view: str
    conviction: float = Field(ge=0.0, le=1.0)
    rationale: str
    source_refs: list[ResearchSourceRef] = Field(default_factory=list)


class StrategyAssetView(BaseModel):
    asset: str
    final_view: str
    final_conviction: float = Field(ge=0.0, le=1.0)
    internal_view: str | None = None
    external_view: str | None = None
    conflict_resolution: str | None = None
    rationale: str = ""


class StrategyView(BaseModel):
    message_type: Literal["strategy_house_view"] = "strategy_house_view"
    source: StrategyViewSourceLiteral = "stub"
    asset_views: list[StrategyAssetView] = Field(default_factory=list)
    summary: str = ""
    coin_temperature: CoinTemperatureLiteral = Field(
        default=2,
        description=(
            "Crypto enthusiasm dial 1–5 (3=neutral). "
            "From configs.business_guardrails (management Streamlit)."
        ),
    )
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Risk / folio / trades / PDF
# ---------------------------------------------------------------------------


class ClientPortfolioMetrics(BaseModel):
    expected_return: float | None = None
    annualized_volatility: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    max_drawdown: float | None = None
    max_runup: float | None = None
    cvar_95: float | None = None
    scenario_return: float | None = None


class OptiPosition(BaseModel):
    ticker: str
    asset_class: AssetClassLiteral
    quantity: float
    weight_pct: float
    notional_value: float | None = None
    """USD notional (FX applied for HKD names)."""
    spot_price: float | None = None
    """Local last price (HKD for *.HK, USD otherwise) — not FX-converted."""
    currency: Literal["USD", "HKD"] = "USD"
    """Quote currency of ``spot_price`` / last price."""


class StagedTrade(BaseModel):
    order_id: str
    client_alias: str | None = None
    status: Literal["Pending", "Executed"] = "Pending"
    ticker: str
    asset_class: AssetClassLiteral
    side: TradeSideLiteral
    quantity: float
    exec_type: Literal["Market"] = "Market"
    notional_value: float | None = None
    from_weight_pct: float | None = None
    to_weight_pct: float | None = None


class TalkingPoint(BaseModel):
    bullet: str
    driver: str | None = None


class ScenarioParamDiff(BaseModel):
    field_path: str
    agent_value: Any
    human_value: Any
    is_exact_match: bool


# ---------------------------------------------------------------------------
# API request / response
# ---------------------------------------------------------------------------


class ScenarioParseRequest(BaseModel):
    portfolio_id: UUID
    user_prompt: str = Field(..., min_length=1, max_length=4000)


class ScenarioParseResponse(BaseModel):
    scenario_run_id: UUID
    portfolio_id: UUID
    status: Literal["PARSED"] = "PARSED"
    phase: Literal["phase1", "phase2"] = "phase1"
    agent_scenario_params: ScenarioParams
    scenario_params: ScenarioParams
    messages: list[str] = Field(default_factory=list)


class ScenarioUpdateParamsRequest(BaseModel):
    scenario_run_id: UUID
    scenario_params: ScenarioParams


class ScenarioUpdateParamsResponse(BaseModel):
    scenario_run_id: UUID
    status: Literal["PARAMS_UPDATED"] = "PARAMS_UPDATED"
    scenario_params: ScenarioParams
    scenario_params_version: int
    diffs: list[ScenarioParamDiff]
    is_exact_match: bool = Field(
        ...,
        description="True when no human edits vs agent_scenario_params (accuracy KPI)",
    )


class ScenarioRunRequest(BaseModel):
    scenario_run_id: UUID


class ScenarioRunResponse(BaseModel):
    scenario_run_id: UUID
    portfolio_id: UUID
    status: Literal["COMPLETED", "FAILED"]
    phase: Literal["phase1", "phase2"] = "phase1"
    scenario_params: ScenarioParams
    strategy_view: StrategyView | None = None
    base_client_metrics: ClientPortfolioMetrics | None = None
    opti_folio: list[OptiPosition] = Field(default_factory=list)
    opti_client_metrics: ClientPortfolioMetrics | None = None
    metrics_delta: dict[str, float | None] = Field(default_factory=dict)
    staged_trades: list[StagedTrade] = Field(default_factory=list)
    talking_points: list[TalkingPoint] = Field(default_factory=list)
    performance_series: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Indexed cumulative performance (start=100) for Starting Positions "
            "vs Optimized Portfolio over lookback_window."
        ),
    )
    error_code: str | None = None
    error_message: str | None = None
    messages: list[str] = Field(default_factory=list)


class ScenarioPdfRequest(BaseModel):
    scenario_run_id: UUID


class ScenarioPdfResponse(BaseModel):
    scenario_run_id: UUID
    content_type: Literal["application/pdf"] = "application/pdf"
    filename: str
    # Bytes delivered via StreamingResponse / FileResponse; metadata only here
    byte_length: int | None = None
