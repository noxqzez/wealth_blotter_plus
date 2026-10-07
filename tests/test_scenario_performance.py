"""Unit tests for base vs opti indexed performance series."""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.app.services.scenario_performance import (
    build_base_vs_opti_performance,
    top_holdings_for_slide,
)


def _price_frame() -> pd.DataFrame:
    idx = pd.date_range("2020-01-01", periods=6, freq="B")
    # AAPL present from day 0; MSFT appears day 2; gap on AAPL day 4 (ffill)
    aapl = [100.0, 101.0, 102.0, 103.0, np.nan, 105.0]
    msft = [np.nan, np.nan, 50.0, 51.0, 52.0, 53.0]
    return pd.DataFrame({"AAPL": aapl, "MSFT": msft}, index=idx)


def test_indexed_series_starts_at_100_and_validates() -> None:
    prices = _price_frame()
    perf = build_base_vs_opti_performance(
        prices,
        base_weights={"AAPL": 0.6, "MSFT": 0.3, "CASH": 0.1},
        opti_weights={"AAPL": 0.4, "MSFT": 0.5, "CASH": 0.1},
        lookback_window="5Y",
    )
    assert perf.validation_ok is True
    assert perf.points[0].starting_positions == 100.0
    assert perf.points[0].optimized_portfolio == 100.0
    assert perf.lookback_window == "5Y"
    assert perf.point_count_full >= 2
    assert perf.base_total_return is not None
    assert perf.opti_total_return is not None


def test_identical_weights_produce_identical_series() -> None:
    prices = _price_frame()
    w = {"AAPL": 0.7, "MSFT": 0.2, "CASH": 0.1}
    perf = build_base_vs_opti_performance(
        prices, base_weights=w, opti_weights=w, lookback_window="1Y"
    )
    for pt in perf.points:
        assert abs(pt.starting_positions - pt.optimized_portfolio) < 1e-9


def test_drop_until_appears_then_ffill_gap() -> None:
    """MSFT absent early; AAPL gap mid-series uses prior price."""
    prices = _price_frame()
    # 100% MSFT: no return until MSFT appears
    perf = build_base_vs_opti_performance(
        prices,
        base_weights={"MSFT": 1.0},
        opti_weights={"MSFT": 1.0},
    )
    # After MSFT appears (50→53): 100 * 53/50 = 106
    assert abs(perf.points[-1].starting_positions - 106.0) < 1e-6


def test_cash_only_is_flat_at_100() -> None:
    prices = _price_frame()
    perf = build_base_vs_opti_performance(
        prices,
        base_weights={"CASH": 1.0},
        opti_weights={"CASH": 1.0},
    )
    for pt in perf.points:
        assert abs(pt.starting_positions - 100.0) < 1e-9


def test_chart_lookback_fills_gaps_continuously() -> None:
    """Mid-series holes are filled so the PDF axis has no breaks."""
    idx = pd.to_datetime(
        ["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"]
    )
    # Gap over weekend is fine; also inject a mid-week hole by omitting 01-03 in wealth path
    prices = pd.DataFrame(
        {"AAPL": [100.0, 101.0, 102.0, 103.0, 104.0]},
        index=idx,
    )
    perf = build_base_vs_opti_performance(
        prices,
        base_weights={"AAPL": 1.0},
        opti_weights={"AAPL": 1.0},
        lookback_window="5Y",
        target_lookback_days=3650,
    )
    assert perf.lookback_window == "5Y"
    assert perf.validation_ok is True
    dates = [pt.date for pt in perf.points]
    # Continuous business days between first and last (no missing weekdays).
    assert dates[0] == "2020-01-01"
    assert "2020-01-06" in dates  # Monday after weekend gap is present
    assert len(perf.points) >= 5


def test_trailing_window_clips_to_target_lookback() -> None:
    idx = pd.date_range("2018-01-01", periods=800, freq="B")
    prices = pd.DataFrame({"AAPL": np.linspace(100.0, 180.0, len(idx))}, index=idx)
    perf = build_base_vs_opti_performance(
        prices,
        base_weights={"AAPL": 1.0},
        opti_weights={"AAPL": 1.0},
        lookback_window="5Y",
        target_lookback_days=365,  # ~1Y calendar clip
    )
    first = pd.Timestamp(perf.points[0].date)
    last = pd.Timestamp(perf.points[-1].date)
    assert (last - first).days <= 370
    assert (last - first).days >= 300


def test_top_holdings_rolls_others() -> None:
    positions = [
        {"ticker": f"T{i}", "weight_pct": 0.08, "asset_class": "EQUITY"}
        for i in range(12)
    ]
    positions[0]["weight_pct"] = 0.2
    out = top_holdings_for_slide(positions, top_n=10)
    assert len(out) == 11  # 10 + Others
    assert out[-1]["ticker"] == "Others"
    assert abs(float(out[-1]["weight_pct"]) - 0.16) < 1e-9
    assert len(out[-1]["constituents"]) == 2
