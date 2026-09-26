"""Unit tests for WF-02 risk_engine (deterministic, no network)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.app.services.risk_engine import (
    RiskEngineError,
    compute_client_metrics,
    compute_client_metrics_from_weights,
    metrics_delta,
    portfolio_daily_returns,
    quant_risk_feed,
)


def _synthetic_returns(n: int = 252) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    idx = pd.bdate_range("2023-01-01", periods=n)
    aapl = rng.normal(0.0005, 0.01, size=n)
    msft = rng.normal(0.0004, 0.009, size=n)
    btc = rng.normal(0.001, 0.03, size=n)
    return pd.DataFrame({"AAPL": aapl, "MSFT": msft, "BTC-USD": btc}, index=idx)


def test_portfolio_returns_and_metrics() -> None:
    rets = _synthetic_returns()
    weights = {"AAPL": 0.5, "MSFT": 0.4, "BTC-USD": 0.1}
    port = portfolio_daily_returns(rets, weights)
    assert len(port) == len(rets)
    metrics = compute_client_metrics(port)
    assert metrics.expected_return is not None
    assert metrics.annualized_volatility is not None
    assert metrics.annualized_volatility > 0
    assert metrics.max_drawdown is not None
    assert metrics.max_drawdown <= 0
    assert metrics.cvar_95 is not None


def test_metrics_from_weights_and_delta() -> None:
    rets = _synthetic_returns()
    base_w = {"AAPL": 0.6, "MSFT": 0.4, "BTC-USD": 0.0}
    opti_w = {"AAPL": 0.5, "MSFT": 0.4, "BTC-USD": 0.1}
    base = compute_client_metrics_from_weights(rets, base_w)
    opti = compute_client_metrics_from_weights(rets, opti_w)
    delta = metrics_delta(base, opti)
    assert "sharpe_ratio" in delta
    assert "expected_return" in delta


def test_quant_risk_feed_shape() -> None:
    rets = _synthetic_returns(60)
    feed = quant_risk_feed(rets)
    assert set(feed["tickers"]) == {"AAPL", "MSFT", "BTC-USD"}
    assert "AAPL" in feed["expected_returns_annual"]  # type: ignore[operator]
    assert feed["n_observations"] == 60


def test_empty_returns_raises() -> None:
    with pytest.raises(RiskEngineError):
        portfolio_daily_returns(pd.DataFrame(), {"AAPL": 1.0})
