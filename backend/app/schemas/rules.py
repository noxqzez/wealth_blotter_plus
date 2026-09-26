"""Rules-as-code configuration models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MarketDataLimits(BaseModel):
    """
    Outbound provider throttle for Yahoo / future crypto history / news.
    Separate from inbound FastAPI `api_rate_limit_rpm`.
    """

    # Yahoo Finance (spot / MVP)
    yahoo_outbound_rpm: int = Field(default=30, ge=1)
    yahoo_burst_calls: int = Field(default=10, ge=1)
    yahoo_burst_window_seconds: int = Field(default=10, ge=1)
    yahoo_max_concurrency: int = Field(default=2, ge=1, le=8)
    yahoo_min_spacing_ms: int = Field(default=250, ge=0)
    yahoo_cache_ttl_seconds: int = Field(default=120, ge=0)
    yahoo_max_calls_per_agent_run: int = Field(default=20, ge=1)
    yahoo_max_retries: int = Field(default=3, ge=0, le=5)

    # Crypto historic prices (later)
    crypto_history_outbound_rpm: int = Field(default=20, ge=1)
    crypto_history_burst_calls: int = Field(default=5, ge=1)
    crypto_history_burst_window_seconds: int = Field(default=10, ge=1)
    crypto_history_max_concurrency: int = Field(default=1, ge=1, le=4)
    crypto_history_min_spacing_ms: int = Field(default=500, ge=0)
    crypto_history_cache_ttl_seconds: int = Field(default=300, ge=0)
    crypto_history_max_symbols_per_request: int = Field(default=10, ge=1)
    crypto_history_max_lookback_days: int = Field(default=90, ge=1)
    crypto_history_max_calls_per_agent_run: int = Field(default=10, ge=1)
    crypto_history_max_retries: int = Field(default=3, ge=0, le=5)

    # News APIs (later)
    news_outbound_rpm: int = Field(default=10, ge=1)
    news_burst_calls: int = Field(default=3, ge=1)
    news_burst_window_seconds: int = Field(default=10, ge=1)
    news_max_concurrency: int = Field(default=1, ge=1, le=4)
    news_min_spacing_ms: int = Field(default=1000, ge=0)
    news_cache_ttl_seconds: int = Field(default=600, ge=0)
    news_max_articles_per_query: int = Field(default=10, ge=1)
    news_max_calls_per_agent_run: int = Field(default=5, ge=1)
    news_max_retries: int = Field(default=3, ge=0, le=5)


class TechnicalLimits(BaseModel):
    """Engineering-managed limits enforced at the FastAPI boundary."""

    max_tickers_per_request: int = Field(default=50, ge=1)
    api_rate_limit_rpm: int = Field(default=120, ge=1)
    execution_timeout_seconds: int = Field(default=120, ge=1)
    max_batch_ingest_rows: int = Field(default=500, ge=1)
    max_self_heal_retries: int = Field(default=3, ge=0, le=5)
    market_data: MarketDataLimits = Field(default_factory=MarketDataLimits)


class JurisdictionRules(BaseModel):
    """Firm jurisdiction / market eligibility (stored on CLIENT trading_rules)."""

    sfc_enabled: bool = True
    sec_enabled: bool = True
    allowed_markets: list[str] = Field(default_factory=lambda: ["us", "hk", "crypto"])


class BusinessGuardrails(BaseModel):
    """Soft product knobs only — weight/universe/jurisdiction live in trading_rules."""

    confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    coin_temperature: int = Field(
        default=2,
        ge=1,
        le=5,
        description="Crypto enthusiasm dial 1–5 (3=neutral). Default 2 until dashboard exists.",
    )
