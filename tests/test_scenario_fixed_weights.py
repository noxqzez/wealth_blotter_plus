"""Optimizer freeze list + weight overrides + risk_aversion wiring."""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.app.services.scenario_optimizer import (
    DEFAULT_RISK_AVERSION,
    OptimizeConstraints,
    _resolve_fixed_weights,
    optimize_weights,
)


def _toy_returns() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    n = 60
    cols = ["AAPL", "MSFT", "CASH_USD"]
    data = rng.normal(0.0004, 0.01, size=(n, 3))
    data[:, 2] = 0.0
    return pd.DataFrame(data, columns=cols)


def test_resolve_fixed_weights_freeze_and_override() -> None:
    base = [
        {"ticker": "AAPL", "current_weight_pct": 0.20},
        {"ticker": "MSFT", "current_weight_pct": 0.30},
        {"ticker": "CASH_USD", "current_weight_pct": 0.50},
    ]
    fixed = _resolve_fixed_weights(
        base_positions=base,
        freeze_list=["AAPL"],
        weight_overrides={"MSFT": 0.25},
        universe_tickers=["AAPL", "MSFT", "CASH_USD"],
    )
    assert fixed["AAPL"] == 0.20
    assert fixed["MSFT"] == 0.25


def test_resolve_accepts_percent_overrides() -> None:
    fixed = _resolve_fixed_weights(
        base_positions=[],
        freeze_list=[],
        weight_overrides={"AAPL": 15},  # percent form
        universe_tickers=["AAPL", "CASH_USD"],
    )
    assert abs(fixed["AAPL"] - 0.15) < 1e-9


def test_optimize_honors_fixed_weights() -> None:
    rets = _toy_returns()
    cov = np.cov(rets.to_numpy().T) * 252.0
    mu = rets.mean().to_numpy() * 252.0
    cons = OptimizeConstraints(
        cash_tickers={"CASH_USD"},
        risk_aversion=DEFAULT_RISK_AVERSION,
        fixed_weights={"AAPL": 0.15},
        min_cash_weight=0.05,
    )
    w = optimize_weights(
        expected_returns=mu,
        cov=cov,
        constraints=cons,
        tickers=list(rets.columns),
    )
    assert abs(w.get("AAPL", 0.0) - 0.15) < 1e-5
    assert abs(sum(w.values()) - 1.0) < 1e-6


def test_higher_risk_aversion_shifts_toward_cash() -> None:
    rets = _toy_returns()
    cov = np.cov(rets.to_numpy().T) * 252.0
    mu = np.array([0.12, 0.10, 0.0])
    tickers = list(rets.columns)

    aggressive = optimize_weights(
        expected_returns=mu,
        cov=cov,
        constraints=OptimizeConstraints(
            cash_tickers={"CASH_USD"}, risk_aversion=0.5
        ),
        tickers=tickers,
    )
    conservative = optimize_weights(
        expected_returns=mu,
        cov=cov,
        constraints=OptimizeConstraints(
            cash_tickers={"CASH_USD"}, risk_aversion=10.0
        ),
        tickers=tickers,
    )
    assert conservative.get("CASH_USD", 0.0) >= aggressive.get("CASH_USD", 0.0) - 1e-9
