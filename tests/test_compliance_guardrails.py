"""Compliance guardrail tests driven by ``tests/golden/compliance_guardrails.yaml``."""

from __future__ import annotations

import pytest

from backend.app.agents.tools.compliance_tool import evaluate_orders_against_guardrails
from backend.app.schemas.agent_outputs import AssetClass, ProposedOrder
from backend.app.services.trading_rules_policy import max_single_name_weight_from_rules
from tests.golden.loader import case_ids, load_golden

_CASES = load_golden("compliance_guardrails")


def _orders(raw: list[dict] | None) -> list[ProposedOrder]:
    out: list[ProposedOrder] = []
    for item in raw or []:
        ac_raw = str(item.get("asset_class") or "other")
        try:
            ac = AssetClass(ac_raw)
        except ValueError:
            ac = AssetClass.OTHER
        out.append(
            ProposedOrder(
                symbol=str(item["symbol"]),
                side=str(item.get("side") or "buy"),
                quantity=float(item["quantity"]),
                asset_class=ac,
            )
        )
    return out


@pytest.mark.parametrize("case", _CASES, ids=case_ids(_CASES))
def test_golden_compliance_guardrails(case: dict) -> None:
    expect = case["expect"]
    inp = case.get("input") or {}
    rules = list(inp.get("trading_rules") or [])

    if "max_single_from_rules" in expect:
        assert max_single_name_weight_from_rules(rules) == pytest.approx(
            float(expect["max_single_from_rules"])
        )
        return

    result = evaluate_orders_against_guardrails(
        _orders(inp.get("orders")),
        trading_rules=rules,
        current_cash_pct=float(inp.get("current_cash_pct") or 0.0),
        current_crypto_pct=float(inp.get("current_crypto_pct") or 0.0),
        single_name_weights={
            str(k): float(v) for k, v in (inp.get("single_name_weights") or {}).items()
        },
    )

    if "passed" in expect:
        assert result.passed is bool(expect["passed"])
    needle = expect.get("violation_contains")
    if needle:
        assert any(needle in v for v in result.violations), result.violations
    if "confidence_min" in expect:
        assert result.confidence_score >= float(expect["confidence_min"])
    if "confidence_max" in expect:
        assert result.confidence_score <= float(expect["confidence_max"])
