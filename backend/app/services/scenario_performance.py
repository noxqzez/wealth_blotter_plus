"""Base vs optimized portfolio cumulative performance (indexed to 100).

Daily-rebalanced to constant target weights over a continuous lookback window.
Default chart window is 5Y regardless of shorter optimizer / risk windows.

Missing prices after a ticker appears are forward-filled; tickers are excluded
until their first valid price (weights renormalized among available names).
Cash contributes return 0. Calendar gaps are filled so the PDF series has no
breaks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

_CASH_ALIASES: frozenset[str] = frozenset({"CASH", "CASH_USD", "USD", "HKD"})
_START_INDEX: float = 100.0
_TERMINAL_TOL: float = 1e-6
_MAX_CHART_POINTS: int = 180

# PDF / client chart always spans this window when history is available.
DEFAULT_CHART_LOOKBACK_WINDOW: str = "5Y"
DEFAULT_CHART_LOOKBACK_DAYS: int = 1825  # ~5Y calendar (matches scenario_history)


class PerformancePoint(BaseModel):
    date: str
    starting_positions: float
    optimized_portfolio: float


class PortfolioPerformanceSeries(BaseModel):
    """Persisted / PDF chart payload for Starting Positions vs Optimized."""

    lookback_window: str | None = None
    start_index: float = _START_INDEX
    label_base: str = "Starting Positions"
    label_opti: str = "Optimized Portfolio"
    points: list[PerformancePoint] = Field(default_factory=list)
    point_count_full: int = 0
    base_total_return: float | None = None
    opti_total_return: float | None = None
    validation_ok: bool = False
    validation_messages: list[str] = Field(default_factory=list)


class PerformanceBuildError(ValueError):
    """Could not build a valid indexed performance series."""


def build_base_vs_opti_performance(
    prices: pd.DataFrame,
    *,
    base_weights: Mapping[str, float],
    opti_weights: Mapping[str, float],
    lookback_window: str | None = None,
    start_index: float = _START_INDEX,
    max_chart_points: int = _MAX_CHART_POINTS,
    target_lookback_days: int | None = None,
) -> PortfolioPerformanceSeries:
    """
    Build indexed cumulative performance for base and opti weight vectors.

    ``prices``: DatetimeIndex, columns = tickers (may include leading NaNs).
    Cash tickers need not appear in ``prices`` (return treated as 0).

    The series is clipped to a trailing ``target_lookback_days`` window (default
    5Y) and reindexed onto a continuous business-day calendar so optimizer
    overlap cuts or mid-series gaps never leave holes in the PDF chart.
    """
    if prices is None or prices.empty:
        raise PerformanceBuildError("Price panel is empty")

    window_label = (lookback_window or DEFAULT_CHART_LOOKBACK_WINDOW).strip() or (
        DEFAULT_CHART_LOOKBACK_WINDOW
    )
    days = (
        int(target_lookback_days)
        if target_lookback_days is not None and target_lookback_days > 0
        else DEFAULT_CHART_LOOKBACK_DAYS
    )

    panel = _clip_to_lookback(_prepare_prices(prices), lookback_days=days)
    if panel.empty or len(panel) < 2:
        raise PerformanceBuildError("Insufficient price history for chart lookback")

    base_idx = _indexed_series(panel, base_weights, start_index=start_index)
    opti_idx = _indexed_series(panel, opti_weights, start_index=start_index)
    aligned = _continuous_aligned_frame(
        base_idx,
        opti_idx,
        calendar_index=panel.index,
        start_index=start_index,
    )
    if aligned.empty or len(aligned) < 2:
        raise PerformanceBuildError("Insufficient overlapping performance dates")

    messages = validate_performance_frame(aligned, start_index=start_index)
    ok = not any(m.startswith("FAIL:") for m in messages)

    full_points = [
        PerformancePoint(
            date=_as_iso_date(idx),
            starting_positions=float(row["starting_positions"]),
            optimized_portfolio=float(row["optimized_portfolio"]),
        )
        for idx, row in aligned.iterrows()
    ]
    chart_points = _downsample_points(full_points, max_points=max_chart_points)

    base_end = float(aligned["starting_positions"].iloc[-1])
    opti_end = float(aligned["optimized_portfolio"].iloc[-1])
    return PortfolioPerformanceSeries(
        lookback_window=window_label,
        start_index=float(start_index),
        points=chart_points,
        point_count_full=len(full_points),
        base_total_return=base_end / float(start_index) - 1.0,
        opti_total_return=opti_end / float(start_index) - 1.0,
        validation_ok=ok,
        validation_messages=messages,
    )


def _clip_to_lookback(prices: pd.DataFrame, *, lookback_days: int) -> pd.DataFrame:
    """Keep the trailing calendar window ending at the last available price date."""
    if prices.empty or lookback_days <= 0:
        return prices
    end = prices.index.max()
    start = end - pd.Timedelta(days=int(lookback_days))
    clipped = prices.loc[prices.index >= start]
    return clipped if not clipped.empty else prices


def _continuous_aligned_frame(
    base_idx: pd.Series,
    opti_idx: pd.Series,
    *,
    calendar_index: pd.Index,
    start_index: float,
) -> pd.DataFrame:
    """Business-day continuous frame; ffill gaps; lead with start_index."""
    if calendar_index.empty:
        return pd.DataFrame(columns=["starting_positions", "optimized_portfolio"])

    cal = pd.bdate_range(calendar_index.min(), calendar_index.max())
    base = base_idx.reindex(cal).ffill()
    opti = opti_idx.reindex(cal).ffill()
    # Days before either series has a first print stay flat at the start index.
    base = base.fillna(float(start_index))
    opti = opti.fillna(float(start_index))
    # Force the window open at start_index so validation stays stable.
    if len(base):
        base.iloc[0] = float(start_index)
        opti.iloc[0] = float(start_index)
    aligned = pd.concat(
        {"starting_positions": base, "optimized_portfolio": opti},
        axis=1,
    )
    return aligned.dropna(how="any")


def _indexed_series(
    prices: pd.DataFrame,
    weights: Mapping[str, float],
    *,
    start_index: float,
) -> pd.Series:
    panel = _prepare_prices(prices)
    daily = _daily_rebalanced_returns(panel, weights)
    if daily.empty or panel.empty:
        return pd.Series(dtype=float)

    wealth = (1.0 + daily).cumprod() * float(start_index)
    start_date = panel.index[0]
    # If the first return date equals the first price date, force that point to
    # start_index and compound from the next observation.
    if daily.index[0] == start_date:
        wealth = wealth / float(1.0 + daily.iloc[0])
        wealth.iloc[0] = float(start_index)
        wealth.name = "index"
        return wealth

    head = pd.Series([float(start_index)], index=[start_date], dtype=float)
    out = pd.concat([head, wealth])
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out.name = "index"
    return out


def _daily_rebalanced_returns(
    prices: pd.DataFrame,
    weights: Mapping[str, float],
) -> pd.Series:
    panel = prices if _looks_prepared(prices) else _prepare_prices(prices)
    w_map = _normalize_weight_keys(weights)
    if not w_map:
        raise PerformanceBuildError("Weight vector is empty")

    for ticker in list(w_map):
        if _is_cash(ticker) and ticker not in panel.columns:
            panel = panel.copy()
            panel[ticker] = 1.0

    cols = list(panel.columns)
    rets = panel.pct_change()

    port: list[float] = []
    idx: list[Any] = []
    for i in range(1, len(panel)):
        dt = panel.index[i]
        prev = panel.iloc[i - 1]
        curr = panel.iloc[i]
        day_ret = rets.iloc[i]
        available: dict[str, float] = {}
        for ticker, w in w_map.items():
            if w == 0.0:
                continue
            if _is_cash(ticker):
                available[ticker] = w
                continue
            if ticker not in cols:
                continue
            p0 = prev.get(ticker)
            p1 = curr.get(ticker)
            if p0 is None or p1 is None:
                continue
            if not np.isfinite(p0) or not np.isfinite(p1) or float(p0) <= 0:
                continue
            available[ticker] = w
        if not available:
            continue
        total = float(sum(available.values()))
        if total <= 0:
            continue
        r_port = 0.0
        for ticker, w in available.items():
            wn = float(w) / total
            if _is_cash(ticker):
                ri = 0.0
            else:
                raw = day_ret.get(ticker)
                ri = float(raw) if raw is not None and np.isfinite(raw) else 0.0
            r_port += wn * ri
        if not np.isfinite(r_port):
            continue
        port.append(float(r_port))
        idx.append(dt)

    return pd.Series(port, index=pd.Index(idx), dtype=float, name="portfolio_return")


def _looks_prepared(prices: pd.DataFrame) -> bool:
    if prices.empty:
        return False
    cols = [str(c) for c in prices.columns]
    return cols == [c.strip().upper() for c in cols]


def _prepare_prices(prices: pd.DataFrame) -> pd.DataFrame:
    clean = prices.copy()
    clean.columns = [str(c).strip().upper() for c in clean.columns]
    clean = clean.apply(pd.to_numeric, errors="coerce").sort_index()
    # Forward-fill gaps after a ticker appears; do not backfill.
    clean = clean.ffill()
    clean = clean.dropna(how="all")
    return clean


def _normalize_weight_keys(weights: Mapping[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for raw, value in weights.items():
        key = str(raw).strip().upper()
        if not key:
            continue
        out[key] = float(value)
    return out


def _is_cash(ticker: str) -> bool:
    return ticker.strip().upper() in _CASH_ALIASES


def validate_performance_frame(
    aligned: pd.DataFrame,
    *,
    start_index: float = _START_INDEX,
) -> list[str]:
    """Return validation messages; those starting with FAIL: are blockers."""
    messages: list[str] = []
    if aligned.empty:
        return ["FAIL: empty performance frame"]

    for col in ("starting_positions", "optimized_portfolio"):
        s = aligned[col]
        if not np.isfinite(s.to_numpy(dtype=float)).all():
            messages.append(f"FAIL: non-finite values in {col}")
        if abs(float(s.iloc[0]) - float(start_index)) > 1e-6:
            messages.append(
                f"FAIL: {col} start {s.iloc[0]:.6f} != {start_index}"
            )
        # Reconstruct terminal from daily returns
        r = s.pct_change().iloc[1:]
        if len(r) and np.isfinite(r.to_numpy(dtype=float)).all():
            rebuilt = float(start_index) * float((1.0 + r).prod())
            if abs(rebuilt - float(s.iloc[-1])) > max(
                _TERMINAL_TOL, abs(float(s.iloc[-1])) * 1e-9
            ):
                messages.append(
                    f"FAIL: {col} terminal {s.iloc[-1]:.6f} != rebuilt {rebuilt:.6f}"
                )
        else:
            messages.append(f"FAIL: {col} has invalid day-over-day returns")

    if not aligned.index.is_monotonic_increasing:
        messages.append("FAIL: dates are not monotonic increasing")

    if not messages:
        messages.append("OK: indexed series start at 100 and compound consistently")
    return messages


def top_holdings_for_slide(
    positions: Sequence[Mapping[str, Any] | Any],
    *,
    top_n: int = 10,
) -> list[dict[str, Any]]:
    """Top N holdings by weight; remainder rolled into Others (for Gamma later)."""
    rows: list[tuple[str, float, str]] = []
    for pos in positions:
        if isinstance(pos, Mapping):
            ticker = str(pos.get("ticker") or "").strip().upper()
            weight = pos.get("weight_pct")
            asset_class = str(pos.get("asset_class") or "").strip().upper()
        else:
            ticker = str(getattr(pos, "ticker", "") or "").strip().upper()
            weight = getattr(pos, "weight_pct", None)
            asset_class = str(getattr(pos, "asset_class", "") or "").strip().upper()
        if not ticker or weight is None or _is_cash(ticker):
            continue
        rows.append((ticker, float(weight), asset_class))
    rows.sort(key=lambda r: r[1], reverse=True)
    top = rows[: max(0, top_n)]
    rest = rows[max(0, top_n) :]
    out: list[dict[str, Any]] = [
        {"ticker": t, "weight_pct": w, "asset_class": ac} for t, w, ac in top
    ]
    if rest:
        out.append(
            {
                "ticker": "Others",
                "weight_pct": float(sum(w for _t, w, _ac in rest)),
                "asset_class": "MIXED",
                "constituents": [t for t, _w, _ac in rest],
            }
        )
    return out


def _downsample_points(
    points: Sequence[PerformancePoint],
    *,
    max_points: int,
) -> list[PerformancePoint]:
    if max_points <= 2 or len(points) <= max_points:
        return list(points)
    # Always keep first/last; sample evenly in between
    n = len(points)
    idxs = sorted(
        {
            0,
            n - 1,
            *[int(round(i * (n - 1) / (max_points - 1))) for i in range(1, max_points - 1)],
        }
    )
    return [points[i] for i in idxs]


def _as_iso_date(value: Any) -> str:
    if hasattr(value, "date"):
        try:
            return value.date().isoformat()  # type: ignore[no-any-return]
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "isoformat"):
        try:
            return str(value.isoformat())[:10]
        except Exception:  # noqa: BLE001
            pass
    return str(value)[:10]


__all__ = [
    "DEFAULT_CHART_LOOKBACK_DAYS",
    "DEFAULT_CHART_LOOKBACK_WINDOW",
    "PerformanceBuildError",
    "PerformancePoint",
    "PortfolioPerformanceSeries",
    "build_base_vs_opti_performance",
    "top_holdings_for_slide",
    "validate_performance_frame",
]
