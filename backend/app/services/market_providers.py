"""Market data providers: crypto history placeholder + Yahoo news for WF-02.

Reuse ``market_throttle`` gates. News uses Yahoo Finance ticker headlines
(broadly available Phase-1 path; no separate news API key required).
"""

from __future__ import annotations

from typing import Any, Sequence

import structlog
import yfinance as yf

from backend.app.schemas.rules import TechnicalLimits
from backend.app.services.market_throttle import (
    MarketDataThrottleError,
    call_with_retries,
    get_throttle_registry,
)

logger = structlog.get_logger(__name__)

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
