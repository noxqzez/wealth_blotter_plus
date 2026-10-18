"""Shared scenario optimizer types, constants, and ticker helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from backend.app.core.error_codes import WBP_SCENARIO_COMPLIANCE, WBP_SCENARIO_RUN
from backend.app.services.trading_rules_policy import DEFAULT_APPROVED_CRYPTO_BASES

# Defaults only — live universe comes from trading_rules PORTFOLIO
# ``approved_crypto_universe`` (see trading_rules_policy).
APPROVED_CRYPTO_BASES: frozenset[str] = frozenset(DEFAULT_APPROVED_CRYPTO_BASES)
APPROVED_CRYPTO_TICKERS: frozenset[str] = frozenset(
    {f"{b}-USD" for b in APPROVED_CRYPTO_BASES}
)

CASH_TICKER_ALIASES: frozenset[str] = frozenset({"CASH", "CASH_USD", "USD"})

TRADING_DAYS_PER_YEAR: int = 252
DEFAULT_RISK_AVERSION: float = 2.5
DEFAULT_TAU: float = 0.05
CASH_COV_EPSILON: float = 1e-12
MIN_VIEW_OMEGA: float = 1e-6
MAX_VIEW_OMEGA: float = 1e2

# Annualized absolute-return tilts added to the prior for qualitative views.
QUALITATIVE_VIEW_TILTS: dict[str, float] = {
    "strongly_bullish": 0.08,
    "very_bullish": 0.08,
    "bullish": 0.04,
    "overweight": 0.03,
    "neutral_to_moderately_positive": 0.015,
    "moderately_positive": 0.015,
    "positive": 0.02,
    "neutral": 0.0,
    "hold": 0.0,
    "mixed": 0.0,
    "neutral_to_cautious": -0.01,
    "cautious": -0.015,
    "negative": -0.02,
    "underweight": -0.03,
    "bearish": -0.04,
    "very_bearish": -0.08,
    "strongly_bearish": -0.08,
}

RULE_PORTFOLIO = "PORTFOLIO"
RULE_TRADE = "TRADE"
RULE_CLIENT = "CLIENT"
RULE_TECHNICAL = "TECHNICAL"
_RULE_CATEGORY_ALIASES: dict[str, str] = {
    "POSITION_LIMIT": RULE_PORTFOLIO,
    "CONCENTRATION": RULE_PORTFOLIO,
    "WEIGHT": RULE_PORTFOLIO,
    "TRADING_RESTRICTION": RULE_TRADE,
    "CLIENT_RESTRICTION": RULE_CLIENT,
    "TECHNICAL": RULE_TECHNICAL,
    "PORTFOLIO": RULE_PORTFOLIO,
    "TRADE": RULE_TRADE,
    "CLIENT": RULE_CLIENT,
}


def normalize_rule_category(raw: str) -> str:
    """Map legacy / free-text categories onto the fixed taxonomy."""
    key = str(raw or "").strip().upper().replace(" ", "_")
    return _RULE_CATEGORY_ALIASES.get(key, key)

TIGHTEN_CRYPTO_FACTOR: float = 0.8
TIGHTEN_SINGLE_FACTOR: float = 0.85
TIGHTEN_CASH_STEP: float = 0.01


class ScenarioOptimizerError(Exception):
    """Structured optimizer failure (no bare except in this module)."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = WBP_SCENARIO_RUN,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "message": self.message,
            "details": self.details,
        }


class ComplianceUnresolvedError(ScenarioOptimizerError):
    """Raised when the compliance re-solve loop exhausts without a feasible book."""

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            error_code=WBP_SCENARIO_COMPLIANCE,
            details=details,
        )


@dataclass
class OptimizeConstraints:
    """Long-only MV constraints (weights are fractions summing to 1)."""

    min_cash_weight: float | None = None
    max_single_weight: float | None = None  # any non-cash name; None = no global cap
    max_crypto_weight: float | None = None
    max_crypto_single_weight: float | None = None  # per CRYPTO name (POSITION_LIMIT)
    restricted_tickers: set[str] = field(default_factory=set)
    cash_tickers: set[str] = field(default_factory=set)
    crypto_tickers: set[str] = field(default_factory=set)
    risk_aversion: float = DEFAULT_RISK_AVERSION
    # Equality locks (fraction of portfolio). Freeze list + advisor overrides.
    fixed_weights: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ComplianceViolation:
    rule_category: str
    rule_name: str
    message: str
    parameters: dict[str, Any] = field(default_factory=dict)


def _normalize_ticker(raw: str) -> str:
    """Optimizer cash form keeps ``CASH_USD``; crypto/equity via shared helper."""
    from backend.app.services.ticker_normalize import normalize_ticker

    t = str(raw or "").strip().upper()
    if t in CASH_TICKER_ALIASES:
        if t == "USD":
            return "USD"
        return "CASH_USD" if t in {"CASH", "CASH_USD"} else t
    return normalize_ticker(t)


def _normalize_asset_class(raw: str) -> str:
    ac = raw.strip().upper()
    if ac in {"COINS", "COIN"}:
        return "CRYPTO"
    if ac in {"EQUITY", "CASH", "CRYPTO"}:
        return ac
    return "EQUITY"


def _is_cash_ticker(ticker: str) -> bool:
    t = ticker.strip().upper()
    return t in CASH_TICKER_ALIASES or t in {"CASH", "CASH_USD", "USD"}


def _is_approved_crypto(ticker: str) -> bool:
    t = _normalize_ticker(ticker)
    if t in APPROVED_CRYPTO_TICKERS:
        return True
    base = t[:-4] if t.endswith("-USD") else t
    return base in APPROVED_CRYPTO_BASES


def _client_rule_applies(params: Mapping[str, Any], client_alias: str | None) -> bool:
    if client_alias is None:
        return not (params.get("client_alias") or params.get("client_id"))
    alias = str(client_alias).strip().lower()
    rule_alias = str(params.get("client_alias") or "").strip().lower()
    if rule_alias and rule_alias == alias:
        return True
    if rule_alias and rule_alias != alias:
        return False
    return not rule_alias
