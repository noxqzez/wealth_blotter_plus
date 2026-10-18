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


# --- MVP UPDATE PORTFOLIO ----------------------------------------------------


class PortfolioPasteRow(BaseModel):
    """Legacy paste row for deprecated POST /portfolio/update."""

    client_alias: str = Field(..., min_length=1, max_length=128)
    ticker: str = Field(..., min_length=1, max_length=32)
    quantity: float = Field(..., ge=0)
    asset_class: str = Field(..., min_length=1, max_length=32)
    currency: str | None = Field(default=None, max_length=8)


class PortfolioUpdateRequest(BaseModel):
    rows: list[PortfolioPasteRow] = Field(..., min_length=1)


class PortfolioPositionRow(BaseModel):
    client_alias: str
    portfolio_id: UUID
    ticker: str
    asset_class: str
    quantity: float
    short_name: str
    spot_price: float
    notional_value: float
    current_weight_pct: float
    valuation_timestamp: str
    currency: str = "USD"
    price_status: str = "VALID"
    pos_id: UUID = Field(
        ...,
        description="Stable PosID (portfolio_positions.id) for audit / Excel reference",
    )


class PortfolioUpdateResponse(BaseModel):
    clients_updated: list[str]
    positions: list[PortfolioPositionRow]


# --- Market EOD history (equity + crypto; scenario audit) ---------------------


class MarketEodPriceBar(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=32)
    asset_class: str = Field(..., min_length=1, max_length=32)
    price_date: str = Field(..., description="ISO date (equity session calendar)")
    close_price: float = Field(..., gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    source_name: str = Field(..., min_length=1, max_length=64)
    provider_symbol: str | None = Field(default=None, max_length=64)


class MarketEodUpsertRequest(BaseModel):
    bars: list[MarketEodPriceBar] = Field(..., min_length=1)


class ScenarioMarketSnapshotRequest(BaseModel):
    scenario_run_id: UUID
    advisor_id: UUID | None = None
    bars: list[MarketEodPriceBar] = Field(..., min_length=1)
    also_upsert_canonical: bool = Field(
        default=True,
        description="Also upsert into market_eod_prices for chart reuse",
    )


# --- RAG research documents --------------------------------------------------


class DocumentIngestJobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    QUARANTINED = "quarantined"
    FAILED = "failed"


class DocumentUploadResponse(BaseModel):
    job_id: UUID
    advisor_id: UUID
    storage_bucket: str
    storage_path: str
    original_filename: str
    status: DocumentIngestJobStatus
    auto_process: bool = False
    page_count: int | None = None


class DocumentIngestJobResponse(BaseModel):
    job_id: UUID
    advisor_id: UUID
    storage_bucket: str
    storage_path: str
    original_filename: str
    status: DocumentIngestJobStatus
    error_message: str | None = None
    document_id: UUID | None = None
    asset_class: str | None = None
    ticker: str | None = None
    chunk_count: int | None = None


class RagRetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    ticker: str | None = Field(default=None, max_length=32)
    asset_class: str | None = Field(
        default=None,
        description="US_Stock | HK_Stock | Crypto",
        max_length=32,
    )
    top_k: int = Field(default=5, ge=1, le=20)


class RagChunkHit(BaseModel):
    chunk_id: UUID
    document_id: UUID
    content: str
    chunk_index: int
    page_number: int | None = None
    ticker: str | None = None
    asset_class: str | None = None
    storage_path: str | None = None
    original_filename: str | None = None
    similarity: float


class RagRetrieveResponse(BaseModel):
    advisor_id: UUID
    query: str
    hits: list[RagChunkHit]
