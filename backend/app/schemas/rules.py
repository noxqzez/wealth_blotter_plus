"""Rules-as-code configuration models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TechnicalLimits(BaseModel):
    """Engineering-managed limits enforced at the FastAPI boundary."""

    max_tickers_per_request: int = Field(default=50, ge=1)
    api_rate_limit_rpm: int = Field(default=120, ge=1)
    execution_timeout_seconds: int = Field(default=120, ge=1)
    max_batch_ingest_rows: int = Field(default=500, ge=1)
    max_self_heal_retries: int = Field(default=3, ge=0, le=5)


class JurisdictionRules(BaseModel):
    sfc_enabled: bool = True
    sec_enabled: bool = True
    allowed_markets: list[str] = Field(default_factory=lambda: ["us", "hk", "crypto"])


class BusinessGuardrails(BaseModel):
    """Product/compliance-managed constraints enforced in CrewAI tools."""

    min_cash_buffer_pct: float = Field(default=0.05, ge=0.0, le=1.0)
    max_single_name_weight_pct: float = Field(default=0.10, ge=0.0, le=1.0)
    max_crypto_weight_pct: float = Field(default=0.05, ge=0.0, le=1.0)
    confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    jurisdictions: JurisdictionRules = Field(default_factory=JurisdictionRules)
