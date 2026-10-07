"""Exec monitor mock economics — keep marketing screenshots in a credible band."""

from __future__ import annotations

from frontend_streamlit.exec_monitor.data import compute_kpis
from frontend_streamlit.exec_monitor.mock_data import mock_bundle


def test_mock_revenue_cost_ratio_is_marketing_viable() -> None:
    bundle = mock_bundle(days=30)
    kpis = compute_kpis(bundle)
    td = bundle["token_daily"]
    cost = float(td["commercial_cost_usd"].sum() + td["open_cost_usd"].sum())
    revenue = float(kpis["trade_revenue"])
    assert revenue > 0
    assert cost > 0
    ratio = revenue / cost
    # Fee revenue (8 bps) / LLM cost — not gross notional / cost.
    assert 10.0 <= ratio <= 30.0, f"unexpected ratio {ratio:.1f}×"
    assert 5_000 <= revenue <= 50_000
    assert kpis["n_trades"] >= 40
    assert kpis["avg_rating"] is not None
    assert 3.5 <= float(kpis["avg_rating"]) <= 5.0
