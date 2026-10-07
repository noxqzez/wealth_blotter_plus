"""Agent-routing ground truth from ``tests/golden/agent_routing.yaml``."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from backend.app.services.scenario_optimizer import qualitative_view_to_tilt
from backend.app.services.scenario_research import consolidate_strategy_view
from backend.app.services.scenario_run import _build_staged_trades
from tests.golden.loader import case_ids, load_golden

_CASES = load_golden("agent_routing")


def _weights(folio: list[dict[str, Any]]) -> dict[str, float]:
    return {
        str(p["ticker"]).upper(): float(p.get("weight_pct") or 0.0) for p in folio if p.get("ticker")
    }


def _qtys(folio: list[dict[str, Any]]) -> dict[str, float]:
    return {
        str(p["ticker"]).upper(): float(p.get("quantity") or 0.0) for p in folio if p.get("ticker")
    }


def _asset_classes(folio: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(p["ticker"]).upper(): str(p.get("asset_class") or "EQUITY").upper()
        for p in folio
        if p.get("ticker")
    }


def _opti_positions(folio: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in folio:
        out.append(
            {
                "ticker": str(p["ticker"]).upper(),
                "asset_class": str(p.get("asset_class") or "EQUITY").upper(),
                "quantity": float(p.get("quantity") or 0.0),
                "weight_pct": float(p.get("weight_pct") or 0.0),
                "notional_value": p.get("notional_value"),
            }
        )
    return out


@pytest.mark.parametrize("case", _CASES, ids=case_ids(_CASES))
def test_golden_agent_routing(case: dict[str, Any]) -> None:
    inp = case.get("input") or {}
    expect = case["expect"]

    view = consolidate_strategy_view(
        house_research=inp.get("house_research") or {},
        market_research=inp.get("market_research") or {},
        universe_tickers=list(inp.get("universe_tickers") or []),
        coin_temperature=inp.get("coin_temperature") or 2,
        scenario_tone=str(inp.get("scenario_tone") or "neutral"),
    )
    by_asset = {v.asset.upper(): v for v in view.asset_views}

    for ticker, rules in (expect.get("asset_views") or {}).items():
        key = str(ticker).upper()
        assert key in by_asset, f"{case['id']}: missing strategist view for {key}"
        asset = by_asset[key]
        allowed = [str(x).lower() for x in (rules.get("final_view_in") or [])]
        if allowed:
            assert asset.final_view.lower() in allowed, (
                f"{case['id']}: {key} final_view={asset.final_view!r} not in {allowed}"
            )
        if "tilt_max" in rules:
            tilt = qualitative_view_to_tilt(asset.final_view)
            assert tilt <= float(rules["tilt_max"]) + 1e-12, (
                f"{case['id']}: {key} tilt {tilt} exceeds max {rules['tilt_max']}"
            )
        if rules.get("house_wins_on_conflict"):
            assert asset.internal_view is not None
            assert asset.external_view is not None
            assert asset.internal_view.lower() != str(asset.external_view).lower()
            assert "prioritized" in (asset.conflict_resolution or "").lower()
            assert asset.final_view.lower() == str(asset.internal_view).lower()
        needle = rules.get("rationale_contains")
        if needle:
            assert needle.lower() in (asset.rationale or "").lower()

    base = list(inp.get("base_folio") or [])
    opti = list(inp.get("opti_folio") or [])
    if not base or not opti:
        return

    base_w = _weights(base)
    opti_w = _weights(opti)
    trades = _build_staged_trades(
        scenario_run_id=uuid4(),
        base_qty=_qtys(base),
        base_weights=base_w,
        opti_positions=_opti_positions(opti),
        asset_classes=_asset_classes(base),
    )

    crypto_exp = expect.get("crypto_orders") or {}
    if crypto_exp.get("sides_only"):
        allowed_sides = {str(s).upper() for s in crypto_exp["sides_only"]}
        crypto_trades = [t for t in trades if str(t.asset_class).upper() == "CRYPTO"]
        assert crypto_trades, f"{case['id']}: expected at least one CRYPTO trade"
        for t in crypto_trades:
            assert str(t.side).upper() in allowed_sides, (
                f"{case['id']}: CRYPTO {t.ticker} side={t.side} not in {allowed_sides}"
            )

    if (expect.get("crypto_weights") or {}).get("opti_lte_base"):
        for ticker, ac in _asset_classes(base).items():
            if ac != "CRYPTO":
                continue
            assert opti_w.get(ticker, 0.0) <= base_w.get(ticker, 0.0) + 1e-9, (
                f"{case['id']}: crypto {ticker} opti {opti_w.get(ticker)} > "
                f"base {base_w.get(ticker)}"
            )

    no_large = (expect.get("orders") or {}).get("no_large_buy")
    if no_large:
        ticker = str(no_large["ticker"]).upper()
        max_inc = float(no_large.get("max_weight_increase") or 0.01)
        delta = opti_w.get(ticker, 0.0) - base_w.get(ticker, 0.0)
        assert delta <= max_inc + 1e-9, (
            f"{case['id']}: {ticker} weight increase {delta:.4f} > {max_inc}"
        )
        buy = next(
            (
                t
                for t in trades
                if t.ticker.upper() == ticker and str(t.side).upper() == "BUY"
            ),
            None,
        )
        if buy is not None:
            # Any residual BUY must be tiny vs book (weight path already capped).
            assert delta <= max_inc + 1e-9
