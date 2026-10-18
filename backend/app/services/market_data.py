"""Yahoo Finance quotes, news, and market-provider helpers.

All outbound Yahoo traffic goes through `market_throttle` (RPM, burst, spacing,
concurrency, TTL cache, optional agent-run budget). Do not call yfinance from
agent tools directly.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Literal, Sequence

import structlog
import yfinance as yf

from backend.app.schemas.rules import TechnicalLimits
from backend.app.services.market_throttle import (
    MarketDataThrottleError,
    call_with_retries,
    get_throttle_registry,
)

logger = structlog.get_logger(__name__)

_PROVIDER = "yahoo"

ListingStatus = Literal["FOUND", "NOT_FOUND", "UNAVAILABLE"]


@dataclass(frozen=True, slots=True)
class MarketQuote:
    spot_price: float
    short_name: str
    listing_status: ListingStatus = "FOUND"


def fetch_market_quotes(
    tickers: list[str],
    *,
    limits: TechnicalLimits | None = None,
) -> dict[str, MarketQuote]:
    """
    Return last price + Yahoo short name per ticker.
    Tickers are queried as provided (e.g. AAPL, 0700.HK, BTC-USD).

    listing_status:
      FOUND — spot_price > 0
      NOT_FOUND — empty/unlisted (caller may drop row)
      UNAVAILABLE — provider error / timeout / missing spot (manual price)
    """
    registry = get_throttle_registry()
    if limits is not None:
        registry.configure(limits)

    md = registry.limits()
    ttl = md.yahoo_cache_ttl_seconds
    max_retries = md.yahoo_max_retries
    max_workers = md.yahoo_max_concurrency

    unique = sorted({t.strip().upper() for t in tickers if t and t.strip()})
    quotes: dict[str, MarketQuote] = {
        t: MarketQuote(spot_price=0.0, short_name=t, listing_status="UNAVAILABLE")
        for t in unique
    }
    if not unique:
        return quotes

    # Serve fully cached tickers without outbound calls.
    pending: list[str] = []
    cache_hits = 0
    for ticker in unique:
        cached = registry.cache.get(_quote_cache_key(ticker))
        if isinstance(cached, MarketQuote):
            quotes[ticker] = cached
            cache_hits += 1
        else:
            pending.append(ticker)

    if not pending:
        logger.info(
            "yahoo_quotes_fetched",
            count=len(quotes),
            cache_hits=cache_hits,
            priced=sum(1 for q in quotes.values() if q.spot_price > 0),
            named=sum(1 for t, q in quotes.items() if q.short_name != t),
            throttled=True,
        )
        return quotes

    try:
        prices, download_ok = _download_prices_with_status(
            pending, max_retries=max_retries
        )
    except Exception as exc:  # noqa: BLE001 — provider outage → UNAVAILABLE
        logger.warning("yahoo_download_provider_failed", error=str(exc), tickers=pending)
        for ticker in pending:
            quotes[ticker] = MarketQuote(
                spot_price=0.0,
                short_name=ticker,
                listing_status="UNAVAILABLE",
            )
            registry.cache.set(_quote_cache_key(ticker), quotes[ticker], ttl)
        return quotes

    names = _fetch_short_names(
        pending,
        max_retries=max_retries,
        max_workers=max_workers,
    )

    for ticker in pending:
        spot = float(prices.get(ticker, 0.0))
        name = names.get(ticker) or ticker
        if spot > 0:
            status: ListingStatus = "FOUND"
        elif not download_ok.get(ticker, False) and name == ticker:
            status = "NOT_FOUND"
        else:
            status = "UNAVAILABLE"
        quote = MarketQuote(spot_price=spot, short_name=name, listing_status=status)
        quotes[ticker] = quote
        registry.cache.set(_quote_cache_key(ticker), quote, ttl)

    logger.info(
        "yahoo_quotes_fetched",
        count=len(quotes),
        cache_hits=cache_hits,
        fetched=len(pending),
        priced=sum(1 for q in quotes.values() if q.spot_price > 0),
        named=sum(1 for t, q in quotes.items() if q.short_name != t),
        throttled=True,
    )
    return quotes


def _quote_cache_key(ticker: str) -> str:
    return f"yahoo:quote:{ticker}"


def _download_prices_with_status(
    unique: list[str],
    *,
    max_retries: int,
) -> tuple[dict[str, float], dict[str, bool]]:
    """
    Returns (prices, had_market_data).
    had_market_data[t] is True when download yielded a close or fast_info price > 0.
    """
    prices: dict[str, float] = {t: 0.0 for t in unique}
    had_data: dict[str, bool] = {t: False for t in unique}

    def _do_download() -> object:
        return yf.download(
            tickers=unique,
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=False,
            progress=False,
        )

    try:
        data = call_with_retries(
            provider=_PROVIDER,
            operation="download",
            fn=_do_download,
            max_retries=max_retries,
            rate_units=1,
        )
    except Exception as exc:  # noqa: BLE001 — re-raise for provider UNAVAILABLE
        logger.warning("yahoo_download_failed", error=str(exc), tickers=unique)
        raise

    if data is None or getattr(data, "empty", True):
        logger.warning("yahoo_download_empty", tickers=unique)
        for ticker in unique:
            fallback = _quote_price_fallback(ticker, max_retries=max_retries)
            prices[ticker] = fallback
            had_data[ticker] = fallback > 0
        return prices, had_data

    for ticker in unique:
        price = _extract_last_close(data, ticker, multi=len(unique) > 1)
        if price is not None and price > 0:
            prices[ticker] = float(price)
            had_data[ticker] = True
        else:
            fallback = _quote_price_fallback(ticker, max_retries=max_retries)
            prices[ticker] = fallback
            had_data[ticker] = fallback > 0
    return prices, had_data


def _download_prices(unique: list[str], *, max_retries: int) -> dict[str, float]:
    """Backward-compatible wrapper."""
    prices, _ = _download_prices_with_status(unique, max_retries=max_retries)
    return prices


def _fetch_short_names(
    tickers: list[str],
    *,
    max_retries: int,
    max_workers: int,
) -> dict[str, str]:
    names: dict[str, str] = {}

    def _one(ticker: str) -> tuple[str, str]:
        def _fetch() -> str:
            info = yf.Ticker(ticker).info or {}
            name = (
                info.get("shortName")
                or info.get("longName")
                or info.get("displayName")
                or ticker
            )
            return str(name).strip() or ticker

        try:
            name = call_with_retries(
                provider=_PROVIDER,
                operation="short_name",
                fn=_fetch,
                max_retries=max_retries,
                rate_units=1,
            )
            return ticker, name
        except Exception as exc:  # noqa: BLE001
            logger.warning("yahoo_short_name_failed", ticker=ticker, error=str(exc))
            return ticker, ticker

    workers = min(max_workers, max(1, len(tickers)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, t) for t in tickers]
        for fut in as_completed(futures):
            ticker, name = fut.result()
            names[ticker] = name
    return names


def _extract_last_close(data: object, ticker: str, *, multi: bool) -> float | None:
    try:
        if not multi:
            closes = data["Close"]  # type: ignore[index]
            series = closes.dropna()
            return float(series.iloc[-1]) if len(series) else None

        if ticker in data.columns.get_level_values(0):  # type: ignore[union-attr]
            closes = data[ticker]["Close"]  # type: ignore[index]
            series = closes.dropna()
            return float(series.iloc[-1]) if len(series) else None
    except Exception:  # noqa: BLE001
        return None
    return None


def _quote_price_fallback(ticker: str, *, max_retries: int) -> float:
    def _fetch() -> float:
        info = yf.Ticker(ticker).fast_info
        last = getattr(info, "last_price", None) or getattr(info, "lastPrice", None)
        if last is not None and float(last) > 0:
            return float(last)
        return 0.0

    try:
        return call_with_retries(
            provider=_PROVIDER,
            operation="quote_fallback",
            fn=_fetch,
            max_retries=max_retries,
            rate_units=1,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("yahoo_quote_fallback_failed", ticker=ticker, error=str(exc))
        return 0.0


# --- News / crypto history (formerly market_providers) -----------------------

_CASH = frozenset({"CASH", "CASH_USD", "USD", "HKD"})


def fetch_crypto_history(
    symbols: list[str],
    *,
    lookback_days: int,
    limits: TechnicalLimits | None = None,
) -> dict[str, object]:
    """Placeholder: wire CoinGecko/etc. behind crypto_history gate later."""
    registry = get_throttle_registry()
    if limits is not None:
        registry.configure(limits)
    md = registry.limits()
    unique = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    if len(unique) > md.crypto_history_max_symbols_per_request:
        raise MarketDataThrottleError(
            "crypto_history_max_symbols_per_request exceeded "
            f"({len(unique)} > {md.crypto_history_max_symbols_per_request})"
        )
    if lookback_days > md.crypto_history_max_lookback_days:
        raise MarketDataThrottleError(
            "crypto_history_max_lookback_days exceeded "
            f"({lookback_days} > {md.crypto_history_max_lookback_days})"
        )
    raise NotImplementedError("crypto historic prices provider not wired yet")


def fetch_news(
    query: str,
    *,
    symbols: Sequence[str] | None = None,
    limits: TechnicalLimits | None = None,
) -> list[dict[str, object]]:
    """Yahoo Finance public news headlines for scenario market research."""
    registry = get_throttle_registry()
    if limits is not None:
        registry.configure(limits)
    md = registry.limits()
    max_articles = int(md.news_max_articles_per_query)
    max_retries = int(md.news_max_retries)

    tickers: list[str] = []
    for s in symbols or []:
        t = str(s or "").strip().upper()
        if t and t not in _CASH:
            tickers.append(t)
    equities = [t for t in tickers if not t.endswith("-USD")][:4]
    cryptos = [t for t in tickers if t.endswith("-USD")][:2]
    selected = equities + cryptos or ["AAPL", "BTC-USD"]

    articles: list[dict[str, object]] = []
    for sym in selected:
        if len(articles) >= max_articles:
            break

        def _do_fetch(symbol: str = sym) -> list[dict[str, Any]]:
            ticker = yf.Ticker(symbol)
            raw = getattr(ticker, "news", None) or []
            out: list[dict[str, Any]] = []
            if not isinstance(raw, list):
                return out
            for item in raw[:max_articles]:
                if not isinstance(item, dict):
                    continue
                content = item.get("content") if isinstance(item.get("content"), dict) else item
                if not isinstance(content, dict):
                    content = item
                title = content.get("title") or content.get("headline") or item.get("title") or ""
                link = ""
                canon = content.get("canonicalUrl")
                if isinstance(canon, dict):
                    link = str(canon.get("url") or "")
                else:
                    link = str(content.get("link") or item.get("link") or "")
                publisher = ""
                provider = content.get("provider")
                if isinstance(provider, dict):
                    publisher = str(provider.get("displayName") or "")
                else:
                    publisher = str(content.get("publisher") or item.get("publisher") or "")
                if title:
                    out.append(
                        {
                            "symbol": symbol,
                            "title": str(title),
                            "link": link,
                            "publisher": publisher,
                            "query": query,
                        }
                    )
            return out

        try:
            batch = call_with_retries(
                provider="news",
                operation="yahoo_news",
                fn=_do_fetch,
                max_retries=max_retries,
            )
            articles.extend(batch)
        except Exception as exc:  # noqa: BLE001
            logger.warning("yahoo_news_fetch_failed", symbol=sym, error=str(exc))

    return articles[:max_articles]
