"""Deterministic compliance checks — no LLM for hard thresholds."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from backend.app.schemas.agent_outputs import ComplianceCheckOutput, ProposedOrder
from backend.app.services.trading_rules_policy import (
    jurisdictions_from_rules,
    max_single_name_weight_from_rules,
    min_cash_buffer_from_rules,
)


def evaluate_orders_against_guardrails(
    orders: list[ProposedOrder],
    *,
    trading_rules: Sequence[Mapping[str, Any]],
    current_cash_pct: float,
    current_crypto_pct: float,
    single_name_weights: dict[str, float],
) -> ComplianceCheckOutput:
    """
    Apply active ``trading_rules`` deterministically.

    `single_name_weights` values are fractions (0–1), not percents.
    """
    violations: list[str] = []
    min_cash = min_cash_buffer_from_rules(trading_rules)
    max_single = max_single_name_weight_from_rules(trading_rules)
    jurisdictions = jurisdictions_from_rules(trading_rules)

    if min_cash is not None and current_cash_pct < min_cash:
        violations.append(
            f"trading_rules min_cash_buffer: cash {current_cash_pct:.4f} "
            f"below min_cash_weight {min_cash:.4f}"
        )

    if max_single is not None:
        for symbol, weight in single_name_weights.items():
            if weight > max_single:
                violations.append(
                    f"trading_rules max_single_name_weight: {symbol} "
                    f"{weight:.4f} exceeds max_single_weight {max_single:.4f}"
                )

    allowed = set(jurisdictions.allowed_markets)
    for order in orders:
        market_hint = _market_from_asset_class(order.asset_class.value)
        if market_hint and market_hint not in allowed:
            violations.append(
                f"trading_rules jurisdictions: order {order.symbol} market "
                f"{market_hint} not in allowed_markets"
            )

    passed = len(violations) == 0
    # High confidence: deterministic rule engine, not an LLM guess
    confidence = 0.95 if passed else 0.9
    return ComplianceCheckOutput(
        confidence_score=confidence,
        passed=passed,
        violations=violations,
        rationale="Deterministic trading_rules evaluation",
    )


def _market_from_asset_class(asset_class: str) -> str | None:
    mapping = {
        "equity_us": "us",
        "equity_hk": "hk",
        "crypto": "crypto",
    }
    return mapping.get(asset_class)
