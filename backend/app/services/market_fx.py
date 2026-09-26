"""FX helpers: Yahoo USD/HKD for USD *notionals* only.

HK equities keep **local last price in HKD** on the blotter / opti_folio.
FX (``HKD=X``) is applied only when forming USD notionals and share quantities:

    usd_unit   = hkd_last / hkd_per_usd
    notional   = qty × usd_unit
    opti_qty   = usd_notional_target / usd_unit
"""

from __future__ import annotations

import re
from typing import Mapping

import pandas as pd
import structlog
import yfinance as yf

from backend.app.schemas.rules import TechnicalLimits
from backend.app.services.market_throttle import (
    call_with_retries,
    get_throttle_registry,
)

logger = structlog.get_logger(__name__)

# Peg mid — only used if Yahoo FX is unavailable.
FALLBACK_HKD_PER_USD: float = 7.8
_YAHOO_USD_HKD = "HKD=X"
_YAHOO_HKD_USD = "HKDUSD=X"


class FxRateError(RuntimeError):
    """Yahoo FX fetch failed and no usable rate was available."""


def is_hk_equity_ticker(ticker: str) -> bool:
    t = str(ticker or "").strip().upper()
    return bool(re.fullmatch(r"\d{1,5}\.HK", t))


def currency_for_ticker(ticker: str, currency: str | None = None) -> str:
    """Prefer explicit currency; otherwise infer from ticker suffix."""
    if currency and str(currency).strip():
        return str(currency).strip().upper()
    if is_hk_equity_ticker(ticker):
        return "HKD"
    return "USD"


def fetch_hkd_per_usd(*, limits: TechnicalLimits | None = None) -> float:
    """
    Live HKD per 1 USD from Yahoo (``HKD=X``).

    Falls back to ``FALLBACK_HKD_PER_USD`` if the provider fails.
    """
    registry = get_throttle_registry()
    if limits is not None:
        registry.configure(limits)
    max_retries = int(registry.limits().yahoo_max_retries)

    cache_key = "fx:hkd_per_usd"
    cached = registry.cache.get(cache_key)
    if isinstance(cached, (int, float)) and float(cached) > 0:
        return float(cached)

    def _read_last(symbol: str) -> float | None:
        def _do() -> float | None:
            t = yf.Ticker(symbol)
            # fast_info / history — prefer last close
            hist = t.history(period="5d")
            if hist is not None and not hist.empty and "Close" in hist.columns:
                val = float(hist["Close"].dropna().iloc[-1])
                return val if val > 0 else None
            fi = getattr(t, "fast_info", None)
            if fi is not None:
                for key in ("last_price", "lastPrice", "regular_market_price"):
                    raw = None
                    try:
                        raw = fi[key] if hasattr(fi, "__getitem__") else getattr(fi, key, None)
                    except Exception:  # noqa: BLE001
                        raw = getattr(fi, key, None) if not hasattr(fi, "__getitem__") else None
                    if raw is not None and float(raw) > 0:
                        return float(raw)
            return None

        return call_with_retries(
            provider="yahoo",
            operation="fx",
            fn=_do,
            max_retries=max_retries,
        )

    rate: float | None = None
    try:
        usd_hkd = _read_last(_YAHOO_USD_HKD)
        if usd_hkd is not None and 5.0 < usd_hkd < 12.0:
            rate = float(usd_hkd)
        else:
            hkd_usd = _read_last(_YAHOO_HKD_USD)
            if hkd_usd is not None and 0.05 < hkd_usd < 0.3:
                rate = 1.0 / float(hkd_usd)
    except Exception as exc:  # noqa: BLE001
        logger.warning("yahoo_fx_fetch_failed", error=str(exc))

    if rate is None or rate <= 0:
        logger.warning(
            "yahoo_fx_using_fallback",
            fallback=FALLBACK_HKD_PER_USD,
            symbol=_YAHOO_USD_HKD,
        )
        rate = FALLBACK_HKD_PER_USD
    else:
        logger.info("yahoo_fx_hkd_per_usd", rate=rate, symbol=_YAHOO_USD_HKD)
        ttl = int(registry.limits().yahoo_cache_ttl_seconds)
        registry.cache.set(cache_key, rate, ttl_seconds=ttl)

    return float(rate)


def to_usd(
    local_spot: float,
    currency: str,
    *,
    hkd_per_usd: float | None = None,
    limits: TechnicalLimits | None = None,
) -> float:
    """Convert a local-ccy last price to a USD *unit* price (for notional only)."""
    ccy = (currency or "USD").strip().upper()
    px = float(local_spot)
    if ccy != "HKD":
        return px
    rate = float(hkd_per_usd) if hkd_per_usd and hkd_per_usd > 0 else fetch_hkd_per_usd(limits=limits)
    return px / rate


def notional_usd(
    quantity: float,
    local_spot: float,
    currency: str,
    *,
    hkd_per_usd: float | None = None,
    limits: TechnicalLimits | None = None,
) -> float:
    """USD notional = qty × local_spot, with HKD→USD FX applied to the unit price only."""
    unit_usd = to_usd(local_spot, currency, hkd_per_usd=hkd_per_usd, limits=limits)
    return float(quantity) * float(unit_usd)


def qty_from_usd_notional(
    notional_usd_value: float,
    local_spot: float,
    currency: str,
    *,
    hkd_per_usd: float | None = None,
    limits: TechnicalLimits | None = None,
) -> float:
    """Shares from a USD notional target given a local last price."""
    unit_usd = to_usd(local_spot, currency, hkd_per_usd=hkd_per_usd, limits=limits)
    if unit_usd <= 0:
        return 0.0
    return float(notional_usd_value) / unit_usd


def convert_price_map_to_usd(
    prices: Mapping[str, float],
    *,
    ticker_currency: Mapping[str, str] | None = None,
    hkd_per_usd: float | None = None,
    limits: TechnicalLimits | None = None,
) -> dict[str, float]:
    """Ensure price map values are USD (convert ``*.HK`` / HKD rows)."""
    rate = hkd_per_usd
    needs_fx = False
    for t, px in prices.items():
        ccy = currency_for_ticker(t, (ticker_currency or {}).get(t))
        if ccy == "HKD" and float(px) > 0:
            needs_fx = True
            break
    if needs_fx and (rate is None or rate <= 0):
        rate = fetch_hkd_per_usd(limits=limits)

    out: dict[str, float] = {}
    for t, px in prices.items():
        ccy = currency_for_ticker(t, (ticker_currency or {}).get(t))
        out[str(t).strip().upper()] = to_usd(
            float(px), ccy, hkd_per_usd=rate, limits=limits
        )
    return out


def scale_hk_columns_to_usd(
    prices: pd.DataFrame,
    *,
    hkd_per_usd: float | None = None,
    limits: TechnicalLimits | None = None,
) -> tuple[pd.DataFrame, float | None]:
    """
    Divide ``*.HK`` columns by HKD-per-USD so the frame is USD-denominated.

    Returns (scaled_frame, rate_used_or_None).
    """
    hk_cols = [c for c in prices.columns if is_hk_equity_ticker(str(c))]
    if not hk_cols:
        return prices, None
    rate = float(hkd_per_usd) if hkd_per_usd and hkd_per_usd > 0 else fetch_hkd_per_usd(limits=limits)
    out = prices.copy()
    for col in hk_cols:
        out[col] = out[col] / rate
    return out, rate
