"""WF-02 deterministic risk metrics (no LLM).

Computes client-facing portfolio metrics and optimizer-ready risk feeds from
daily returns. Annualization uses 252 trading days.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from backend.app.schemas.scenario import ClientPortfolioMetrics

TRADING_DAYS_PER_YEAR: int = 252
RISK_FREE_DAILY: float = 0.0
CVAR_ALPHA: float = 0.95


class RiskEngineError(ValueError):
    """Invalid inputs for risk computation."""


def align_weights(
    weights: Mapping[str, float],
    columns: Sequence[str],
    *,
    renormalize: bool = True,
) -> pd.Series:
    """Map weights onto ``columns``; missing tickers get 0. Optionally renorm."""
    w = pd.Series(0.0, index=list(columns), dtype=float)
    for raw_key, value in weights.items():
        key = str(raw_key).strip().upper()
        if key in w.index:
            w.loc[key] = float(value)
    total = float(w.sum())
    if renormalize and total > 0:
        w = w / total
    return w


def portfolio_daily_returns(
    returns: pd.DataFrame,
    weights: Mapping[str, float],
    *,
    renormalize: bool = True,
) -> pd.Series:
    """
    Weighted portfolio daily returns.

    ``returns`` columns are tickers; rows are period (daily) simple returns.
    Cash columns with all-NaN / zeros are fine (contribute 0).
    """
    if returns.empty:
        raise RiskEngineError("returns DataFrame is empty")
    clean = returns.copy()
    clean.columns = [str(c).strip().upper() for c in clean.columns]
    clean = clean.apply(pd.to_numeric, errors="coerce").fillna(0.0)
    w = align_weights(weights, list(clean.columns), renormalize=renormalize)
    port = clean.mul(w, axis=1).sum(axis=1)
    port.name = "portfolio_return"
    return port


def compute_client_metrics(
    portfolio_returns: pd.Series,
    *,
    scenario_return: float | None = None,
    risk_free_daily: float = RISK_FREE_DAILY,
) -> ClientPortfolioMetrics:
    """Client content metrics from a portfolio daily-return series."""
    r = pd.to_numeric(portfolio_returns, errors="coerce").dropna()
    if r.empty:
        return ClientPortfolioMetrics()

    mean_d = float(r.mean())
    vol_d = float(r.std(ddof=1)) if len(r) > 1 else 0.0
    expected_return = mean_d * TRADING_DAYS_PER_YEAR
    ann_vol = vol_d * np.sqrt(TRADING_DAYS_PER_YEAR)

    excess = r - risk_free_daily
    sharpe = (
        float(excess.mean() / excess.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))
        if excess.std(ddof=1) > 0
        else None
    )

    downside = excess.copy()
    downside[downside > 0] = 0.0
    downside_std = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    sortino = (
        float(excess.mean() / downside_std * np.sqrt(TRADING_DAYS_PER_YEAR))
        if downside_std > 0
        else None
    )

    wealth = (1.0 + r).cumprod()
    peak = wealth.cummax()
    drawdown = wealth / peak - 1.0
    max_drawdown = float(drawdown.min()) if len(drawdown) else None

    trough = wealth.cummin()
    runup = wealth / trough - 1.0
    max_runup = float(runup.max()) if len(runup) else None

    cvar = _cvar_left_tail(r, alpha=CVAR_ALPHA)

    return ClientPortfolioMetrics(
        expected_return=expected_return,
        annualized_volatility=ann_vol if ann_vol > 0 else 0.0,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        max_drawdown=max_drawdown,
        max_runup=max_runup,
        cvar_95=cvar,
        scenario_return=scenario_return,
    )


def compute_client_metrics_from_weights(
    returns: pd.DataFrame,
    weights: Mapping[str, float],
    *,
    scenario_return: float | None = None,
) -> ClientPortfolioMetrics:
    """Convenience: weights + asset returns → client metrics."""
    port = portfolio_daily_returns(returns, weights)
    return compute_client_metrics(port, scenario_return=scenario_return)


def metrics_delta(
    base: ClientPortfolioMetrics,
    opti: ClientPortfolioMetrics,
) -> dict[str, float | None]:
    """opti − base for each numeric metric field."""
    out: dict[str, float | None] = {}
    for field_name in ClientPortfolioMetrics.model_fields:
        b = getattr(base, field_name)
        o = getattr(opti, field_name)
        if b is None or o is None:
            out[field_name] = None
        else:
            out[field_name] = float(o) - float(b)
    return out


def quant_risk_feed(returns: pd.DataFrame) -> dict[str, object]:
    """
    Optimizer-ready risk feed (deterministic).

    Returns annualized mean vector, annualized vols, correlation, and sample
    covariance of *daily* returns (caller may replace cov with Ledoit–Wolf).
    """
    if returns.empty:
        raise RiskEngineError("returns DataFrame is empty")
    clean = returns.copy()
    clean.columns = [str(c).strip().upper() for c in clean.columns]
    clean = clean.apply(pd.to_numeric, errors="coerce").fillna(0.0)

    mean_d = clean.mean()
    cov_d = clean.cov(ddof=1)
    vol_d = clean.std(ddof=1)
    corr = clean.corr()

    return {
        "tickers": list(clean.columns),
        "expected_returns_annual": (mean_d * TRADING_DAYS_PER_YEAR).to_dict(),
        "volatilities_annual": (vol_d * np.sqrt(TRADING_DAYS_PER_YEAR)).to_dict(),
        "correlation": corr.replace({np.nan: 0.0}).to_dict(),
        "covariance_daily": cov_d.replace({np.nan: 0.0}).to_dict(),
        "n_observations": int(len(clean)),
    }


def _cvar_left_tail(returns: pd.Series, *, alpha: float) -> float | None:
    """Expected shortfall of the left tail at confidence ``alpha`` (e.g. 0.95)."""
    if returns.empty:
        return None
    q = float(returns.quantile(1.0 - alpha))
    tail = returns[returns <= q]
    if tail.empty:
        return float(q)
    return float(tail.mean())
