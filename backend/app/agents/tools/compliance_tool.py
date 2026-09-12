"""Deterministic compliance checks — no LLM for hard thresholds."""

from __future__ import annotations

from backend.app.schemas.agent_outputs import ComplianceCheckOutput, ProposedOrder
from backend.app.schemas.rules import BusinessGuardrails


def evaluate_orders_against_guardrails(
    orders: list[ProposedOrder],
    *,
    guardrails: BusinessGuardrails,
    current_cash_pct: float,
    current_crypto_pct: float,
    single_name_weights: dict[str, float],
) -> ComplianceCheckOutput:
    """
    Apply business_guardrails deterministically.

    `single_name_weights` values are fractions (0–1), not percents.
    """
    violations: list[str] = []

    if current_cash_pct < guardrails.min_cash_buffer_pct:
        violations.append(
            f"Cash buffer {current_cash_pct:.4f} below minimum "
            f"{guardrails.min_cash_buffer_pct:.4f}"
        )

    if current_crypto_pct > guardrails.max_crypto_weight_pct:
        violations.append(
            f"Crypto weight {current_crypto_pct:.4f} exceeds max "
            f"{guardrails.max_crypto_weight_pct:.4f}"
        )

    for symbol, weight in single_name_weights.items():
        if weight > guardrails.max_single_name_weight_pct:
            violations.append(
                f"Symbol {symbol} weight {weight:.4f} exceeds max "
                f"{guardrails.max_single_name_weight_pct:.4f}"
            )

    allowed = set(guardrails.jurisdictions.allowed_markets)
    for order in orders:
        market_hint = _market_from_asset_class(order.asset_class.value)
        if market_hint and market_hint not in allowed:
            violations.append(f"Order {order.symbol} market {market_hint} not allowed")

    passed = len(violations) == 0
    # High confidence: deterministic rule engine, not an LLM guess
    confidence = 0.95 if passed else 0.9
    return ComplianceCheckOutput(
        confidence_score=confidence,
        passed=passed,
        violations=violations,
        rationale="Deterministic guardrail evaluation",
    )


def _market_from_asset_class(asset_class: str) -> str | None:
    mapping = {
        "equity_us": "us",
        "equity_hk": "hk",
        "crypto": "crypto",
    }
    return mapping.get(asset_class)
