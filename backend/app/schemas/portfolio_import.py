"""Pydantic contracts for securities list import (WF-01)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


AssetClassLiteral = Literal["EQUITY", "CASH", "CRYPTO"]
CurrencyLiteral = Literal["USD", "HKD"]
PriceStatusLiteral = Literal["VALID", "MANUAL_PRICE_REQUIRED"]


class RawImportRow(BaseModel):
    """Advisor paste row (Excel selection)."""

    client_alias: str | None = Field(default=None, max_length=128)
    asset_class: str | None = Field(default=None, max_length=32)
    ticker: str | None = Field(default=None, max_length=32)
    quantity: float | None = None
    currency: str | None = Field(default=None, max_length=8, description="Advisor Ccy")


class ImportValidationRequest(BaseModel):
    rows: list[RawImportRow] = Field(..., min_length=1)


class NormalizedPosition(BaseModel):
    client_alias: str
    asset_class: AssetClassLiteral
    ticker: str
    quantity: float = Field(..., ge=0)
    currency: CurrencyLiteral
    currency_derived: bool = False
    currency_mismatch: bool = False


class DraftPosition(BaseModel):
    """Normalized draft row (no PosID until base promotion)."""

    client_alias: str
    asset_class: AssetClassLiteral
    ticker: str
    quantity: float
    currency: CurrencyLiteral
    currency_derived: bool = False


class ValidateImportResponse(BaseModel):
    portfolio_id: UUID
    draft_id: UUID
    client_alias: str
    folio_stage: Literal["DRAFT"] = "DRAFT"
    positions: list[DraftPosition]
    messages: list[str] = Field(
        default_factory=list,
        description="User-facing normalization notes (also written to audit_events)",
    )


class BuildBaseRequest(BaseModel):
    portfolio_id: UUID
    draft_id: UUID | None = None


class EnrichedPosition(BaseModel):
    pos_id: UUID
    portfolio_id: UUID
    client_alias: str
    asset_class: AssetClassLiteral
    ticker: str
    quantity: float
    spot_price: float
    market_value: float
    currency: CurrencyLiteral
    short_name: str
    current_weight_pct: float
    price_timestamp: datetime | None = None
    status: PriceStatusLiteral


class BuildBaseResponse(BaseModel):
    portfolio_id: UUID
    draft_id: UUID
    client_alias: str
    folio_stage: Literal["BASE"] = "BASE"
    positions: list[EnrichedPosition]
    warnings: list[str] = Field(
        default_factory=list,
        description="User-facing market-data notes (also written to audit_events)",
    )
