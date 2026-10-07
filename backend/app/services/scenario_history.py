"""WF-02 scenario market history: Yahoo EOD → aligned returns for risk/optimize.

Deterministic fetch (no LLM). Crypto uses Yahoo ``*-USD`` symbols; internal
tickers stay as stored on the blotter (e.g. ``BTC-USD``). Cash is skipped for
price history (zero-vol proxy handled by the optimizer).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Mapping, Sequence

import pandas as pd
import structlog
import yfinance as yf

from backend.app.schemas.payload_models import MarketEodPriceBar
from backend.app.schemas.rules import TechnicalLimits
from backend.app.services.market_throttle import (
    call_with_retries,
    get_throttle_registry,
)
from backend.app.services.trading_rules_policy import DEFAULT_APPROVED_CRYPTO_BASES

logger = structlog.get_logger(__name__)

_SOURCE = "yahoo"
_DEFAULT_LOOKBACK_DAYS = 1825  # ~5Y calendar
_CASH_ALIASES = frozenset({"CASH", "CASH_USD", "USD", "HKD"})


class ScenarioHistoryError(ValueError):
    """History fetch / alignment failure."""


@dataclass(frozen=True, slots=True)
class ScenarioHistoryResult:
    """Aligned close prices, daily returns, and EOD bars for persistence."""

    prices: pd.DataFrame
    returns: pd.DataFrame
    bars: list[MarketEodPriceBar]
    provider_symbols: dict[str, str]
    lookback_days: int
    messages: list[str]
    # Sparse panel (ffill only): used for base/opti performance charts.
    # Leading NaNs remain so tickers can be dropped until they appear.
    prices_panel: pd.DataFrame | None = None


def parse_lookback_days(lookback_window: str | None, *, default: int = _DEFAULT_LOOKBACK_DAYS) -> int:
    """Parse ``3Y`` / ``1Y`` / ``6M`` / ``90D`` style windows into calendar days."""
    if not lookback_window or not str(lookback_window).strip():
        return default
    raw = str(lookback_window).strip().upper()
    try:
        if raw.endswith("Y"):
            return int(float(raw[:-1]) * 365)
        if raw.endswith("M"):
            return int(float(raw[:-1]) * 30)
        if raw.endswith("D"):
            return int(float(raw[:-1]))
        return int(raw)
    except ValueError as exc:
        raise ScenarioHistoryError(f"Invalid lookback_window '{lookback_window}'") from exc


def yahoo_symbol_for(ticker: str, asset_class: str) -> str | None:
    """
    Map blotter ticker → Yahoo download symbol.
    Returns None for cash (no price series).
    Crypto eligibility is enforced upstream via trading_rules universe.
    """
    t = ticker.strip().upper()
    ac = asset_class.strip().upper()
    if ac == "CASH" or t in _CASH_ALIASES:
        return None
    if ac == "CRYPTO":
        base = t.removesuffix("-USD").removesuffix("USD")
        return f"{base}-USD"
    return t


def build_scenario_universe(
    base_positions: Sequence[Mapping[str, object]],
    *,
    approved_crypto: Sequence[str] | None = None,
) -> list[tuple[str, str]]:
    """
    Investable universe: base folio + approved coins not already held.

    ``approved_crypto`` is trading_rules ``approved_crypto_universe`` (bases).
    Falls back to DEFAULT_APPROVED_CRYPTO_BASES only if omitted.
    """
    approved = [
        str(c).strip().upper().removesuffix("-USD")
        for c in (approved_crypto if approved_crypto is not None else DEFAULT_APPROVED_CRYPTO_BASES)
        if str(c).strip()
    ]

    seen: dict[str, str] = {}
    for pos in base_positions:
        ticker = str(pos.get("ticker", "")).strip().upper()
        asset_class = str(pos.get("asset_class", "")).strip().upper()
        if not ticker or not asset_class:
            continue
        if asset_class == "CRYPTO":
            base = ticker.removesuffix("-USD")
            if base not in approved:
                continue  # drop non-approved crypto from base
            ticker = f"{base}-USD"
        seen[ticker] = asset_class

    for base in approved:
        key = f"{base}-USD"
        seen.setdefault(key, "CRYPTO")

    return [(t, ac) for t, ac in sorted(seen.items())]


def fetch_scenario_history(
    universe: Sequence[tuple[str, str]],
    *,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    limits: TechnicalLimits | None = None,
    end: date | None = None,
) -> ScenarioHistoryResult:
    """
    Download daily adjusted closes via Yahoo (throttled) and build returns.

    Crypto closes are aligned onto the equity calendar (inner join of dates
    after forward-fill within each series, then intersection).
    """
    if lookback_days <= 0:
        raise ScenarioHistoryError("lookback_days must be positive")

    registry = get_throttle_registry()
    if limits is not None:
        registry.configure(limits)
    md = registry.limits()
    max_retries = md.yahoo_max_retries

    if lookback_days > md.crypto_history_max_lookback_days:
        # Soft warn: technical_limits already raised to 1095 for WF-02
        logger.warning(
            "scenario_history_lookback_above_crypto_cap",
            lookback_days=lookback_days,
            cap=md.crypto_history_max_lookback_days,
        )

    end_d = end or datetime.now(timezone.utc).date()
    start_d = end_d - timedelta(days=lookback_days)

    messages: list[str] = []
    provider_symbols: dict[str, str] = {}
    series: dict[str, pd.Series] = {}
    asset_classes: dict[str, str] = {}

    for ticker, asset_class in universe:
        ysym = yahoo_symbol_for(ticker, asset_class)
        if ysym is None:
            messages.append(f"Skipped cash proxy '{ticker}' for price history")
            continue
        provider_symbols[ticker] = ysym
        asset_classes[ticker] = asset_class.strip().upper()

        try:
            closes = _download_adj_close(
                ysym,
                start=start_d,
                end=end_d + timedelta(days=1),
                max_retries=max_retries,
            )
        except Exception as exc:  # noqa: BLE001 — isolate per ticker
            messages.append(f"History unavailable for {ticker} ({ysym}): {exc}")
            logger.warning(
                "scenario_history_ticker_failed",
                ticker=ticker,
                provider_symbol=ysym,
                error=str(exc),
            )
            continue

        if closes.empty:
            messages.append(f"Empty history for {ticker} ({ysym})")
            continue
        series[ticker] = closes

    if not series:
        raise ScenarioHistoryError("No market history retrieved for scenario universe")

    prices = pd.DataFrame(series).sort_index()
    prices = prices.apply(pd.to_numeric, errors="coerce")
    # Soft align: forward-fill limited gaps; keep leading NaNs for performance.
    prices_soft = prices.ffill(limit=5).dropna(how="all")
    prices_panel = prices_soft.copy()
    # Hard overlap for risk/optimizer covariance (existing behavior).
    prices = prices_soft.dropna(how="any")
    if prices.empty or len(prices) < 5:
        raise ScenarioHistoryError(
            "Insufficient overlapping history after calendar alignment"
        )

    # Keep *.HK closes in HKD (local last price). FX is applied only when
    # forming USD notionals / opti quantities — not on the price series itself.
    returns = prices.pct_change().dropna(how="any")
    bars = _prices_to_bars(prices, asset_classes=asset_classes, provider_symbols=provider_symbols)

    logger.info(
        "scenario_history_fetched",
        tickers=list(prices.columns),
        n_days=len(prices),
        lookback_days=lookback_days,
        bar_count=len(bars),
        panel_days=len(prices_panel),
    )
    return ScenarioHistoryResult(
        prices=prices,
        returns=returns,
        bars=bars,
        provider_symbols=provider_symbols,
        lookback_days=lookback_days,
        messages=messages,
        prices_panel=prices_panel,
    )


def _download_adj_close(
    symbol: str,
    *,
    start: date,
    end: date,
    max_retries: int,
) -> pd.Series:
    registry = get_throttle_registry()

    def _do_download() -> object:
        return yf.download(
            tickers=symbol,
            start=start.isoformat(),
            end=end.isoformat(),
            auto_adjust=True,
            progress=False,
            threads=False,
        )

    raw = call_with_retries(
        provider="yahoo",
        operation="history",
        fn=_do_download,
        max_retries=max_retries,
    )
    if raw is None or (isinstance(raw, pd.DataFrame) and raw.empty):
        return pd.Series(dtype=float)

    df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
    # yfinance may return MultiIndex columns for single ticker
    if isinstance(df.columns, pd.MultiIndex):
        if "Close" in df.columns.get_level_values(0):
            close = df["Close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
        else:
            close = df.iloc[:, 0]
    elif "Close" in df.columns:
        close = df["Close"]
    elif "Adj Close" in df.columns:
        close = df["Adj Close"]
    else:
        close = df.iloc[:, 0]

    series = pd.to_numeric(close, errors="coerce").dropna()
    series.index = pd.to_datetime(series.index).tz_localize(None).normalize()
    series.name = symbol.upper()
    return series


def _prices_to_bars(
    prices: pd.DataFrame,
    *,
    asset_classes: Mapping[str, str],
    provider_symbols: Mapping[str, str],
) -> list[MarketEodPriceBar]:
    bars: list[MarketEodPriceBar] = []
    for ticker in prices.columns:
        ac = asset_classes.get(str(ticker), "EQUITY")
        ysym = provider_symbols.get(str(ticker), str(ticker))
        col = prices[ticker].dropna()
        for ts, px in col.items():
            price = float(px)
            if price <= 0:
                continue
            if hasattr(ts, "date"):
                price_date = ts.date().isoformat()  # type: ignore[no-untyped-call]
            else:
                price_date = str(ts)[:10]
            bars.append(
                MarketEodPriceBar(
                    ticker=str(ticker).upper(),
                    asset_class=ac,
                    price_date=price_date,
                    close_price=price,
                    currency="USD",
                    source_name=_SOURCE,
                    provider_symbol=ysym,
                )
            )
    return bars
