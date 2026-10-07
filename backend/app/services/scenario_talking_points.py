"""Deterministic advisor talking points for scenario PDF / Excel.

Phase-1 content step (no LLM):
1. One performance sentence — top improved metrics only (never worsens).
2. Trade-gated market color — strategist views only for BUY / overweight names.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from backend.app.schemas.scenario import (
    ClientPortfolioMetrics,
    StagedTrade,
    StrategyAssetView,
    StrategyView,
    TalkingPoint,
)

# Higher value is better for the client narrative.
_HIGHER_BETTER: frozenset[str] = frozenset(
    {
        "expected_return",
        "sharpe_ratio",
        "sortino_ratio",
        "max_runup",
        "scenario_return",
    }
)
# Lower value is better.
_LOWER_BETTER: frozenset[str] = frozenset(
    {
        "annualized_volatility",
        "max_drawdown",
        "cvar_95",
    }
)

_METRIC_LABELS: dict[str, str] = {
    "expected_return": "expected return",
    "annualized_volatility": "annualized volatility",
    "sharpe_ratio": "Sharpe ratio",
    "sortino_ratio": "Sortino ratio",
    "max_drawdown": "max drawdown",
    "max_runup": "max run-up",
    "cvar_95": "CVaR 95%",
    "scenario_return": "scenario return",
}

# Align with optimizer qualitative tilts that imply a constructive bias.
_BULLISH_VIEW_TOKENS: frozenset[str] = frozenset(
    {
        "strongly_bullish",
        "very_bullish",
        "bullish",
        "overweight",
        "neutral_to_moderately_positive",
        "moderately_positive",
        "positive",
    }
)
# Cautious / bearish language suitable for underweight / sell talking points.
_CAUTIOUS_VIEW_TOKENS: frozenset[str] = frozenset(
    {
        "neutral_to_cautious",
        "cautious",
        "negative",
        "underweight",
        "bearish",
        "very_bearish",
        "strongly_bearish",
    }
)

_SKIP_SUMMARY_MARKERS: frozenset[str] = frozenset(
    {
        "neutral stub strategy_view for phase-1 uat.",
    }
)

_BASE_EPS = 1e-9
_MIN_IMPROVEMENT_EPS = 1e-12


def build_talking_points(
    *,
    metrics_delta: Mapping[str, float | None],
    base_metrics: ClientPortfolioMetrics | Mapping[str, float | None] | None = None,
    staged_trades: Sequence[StagedTrade | Mapping[str, Any]] | None = None,
    strategy_view: StrategyView | Mapping[str, Any] | None = None,
    opti_folio: Sequence[Mapping[str, Any] | Any] | None = None,
    max_improved_metrics: int = 4,
    max_market_bullets: int = 3,
) -> list[TalkingPoint]:
    """Build PDF/Excel talking points (performance + trade-consistent color)."""
    points: list[TalkingPoint] = []

    perf = _performance_talking_point(
        metrics_delta,
        base_metrics=base_metrics,
        max_metrics=max_improved_metrics,
    )
    points.append(perf)

    sv = _as_strategy_view(strategy_view)
    summary_bullet = _strategy_summary_bullet(sv)
    if summary_bullet is not None:
        points.append(summary_bullet)

    color = _market_color_bullets(
        staged_trades or [],
        sv,
        opti_folio=opti_folio or [],
        max_bullets=max_market_bullets,
    )
    points.extend(color)
    return [
        TalkingPoint(bullet=sanitize_client_commentary(p.bullet), driver=p.driver)
        for p in points
    ]


def extract_news_footnotes(
    articles: Sequence[Mapping[str, Any]] | None,
    *,
    limit: int = 5,
    max_publishers_per_symbol: int = 4,
) -> list[dict[str, Any]]:
    """Compact PDF sources: up to ``limit`` tickers with a few publisher names."""
    if not articles or limit <= 0:
        return []

    # Preserve first-seen symbol order from the article stream.
    by_symbol: dict[str, list[str]] = {}
    order: list[str] = []
    for art in articles:
        if not isinstance(art, Mapping):
            continue
        publisher = str(art.get("publisher") or "").strip()
        if not publisher:
            continue
        symbol = str(art.get("symbol") or "").strip().upper() or "Market"
        if symbol not in by_symbol:
            if len(order) >= limit:
                continue
            by_symbol[symbol] = []
            order.append(symbol)
        pubs = by_symbol[symbol]
        if publisher in pubs:
            continue
        if len(pubs) >= max_publishers_per_symbol:
            continue
        pubs.append(publisher)

    return [
        {"symbol": sym, "publishers": by_symbol[sym]}
        for sym in order
        if by_symbol.get(sym)
    ]


def format_news_footnote_lines(
    footnotes: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    """Compact footprint lines, e.g. ``[1] 0700.HK — Zacks, Quartz``."""
    lines: list[str] = []
    for i, row in enumerate(footnotes or [], start=1):
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol") or "").strip()
        publishers = row.get("publishers")
        if isinstance(publishers, Sequence) and not isinstance(publishers, (str, bytes)):
            names = [str(p).strip() for p in publishers if str(p).strip()]
        else:
            # Legacy per-article rows (title/url) — keep publisher only.
            single = str(row.get("publisher") or "").strip()
            names = [single] if single else []
        if not names:
            continue
        joined = ", ".join(names)
        if symbol:
            lines.append(f"[{i}] {symbol} — {joined}")
        else:
            lines.append(f"[{i}] {joined}")
    return lines


def _talking_points_from_delta(
    delta: Mapping[str, float | None],
) -> list[TalkingPoint]:
    """Backward-compatible entry: performance sentence only."""
    return [
        _performance_talking_point(delta, base_metrics=None, max_metrics=4),
    ]


def _performance_talking_point(
    delta: Mapping[str, float | None],
    *,
    base_metrics: ClientPortfolioMetrics | Mapping[str, float | None] | None,
    max_metrics: int,
) -> TalkingPoint:
    improved = _rank_improvements(delta, base_metrics=base_metrics)
    if not improved:
        return TalkingPoint(
            bullet=(
                "Optimized portfolio generated; no performance metric improvements "
                "versus the existing portfolio were available to highlight."
            ),
            driver="performance_summary",
        )

    top = improved[: max(1, max_metrics)]
    sentence = _format_performance_sentence(top)
    return TalkingPoint(bullet=sentence, driver="performance_summary")


def _rank_improvements(
    delta: Mapping[str, float | None],
    *,
    base_metrics: ClientPortfolioMetrics | Mapping[str, float | None] | None,
) -> list[tuple[str, str, float]]:
    """Return (metric_key, label, improvement_pct) sorted by pct descending."""
    base_map = _as_metrics_map(base_metrics)
    ranked: list[tuple[str, str, float]] = []

    for key, label in _METRIC_LABELS.items():
        raw = delta.get(key)
        if raw is None:
            continue
        d = float(raw)
        if key in _HIGHER_BETTER:
            if d <= _MIN_IMPROVEMENT_EPS:
                continue
            pct = _relative_improvement_pct(d, base_map.get(key), lower_is_better=False)
        elif key in _LOWER_BETTER:
            if d >= -_MIN_IMPROVEMENT_EPS:
                continue
            # Negative delta = improvement for risk metrics
            pct = _relative_improvement_pct(-d, base_map.get(key), lower_is_better=True)
        else:
            continue
        if pct is None or pct <= 0:
            continue
        ranked.append((key, label, pct))

    ranked.sort(key=lambda row: row[2], reverse=True)
    return ranked


def _relative_improvement_pct(
    improvement_abs: float,
    base_value: float | None,
    *,
    lower_is_better: bool,
) -> float | None:
    """Percent improvement vs base; falls back to abs×100 when base ~ 0."""
    del lower_is_better  # reserved for future asymmetric floors
    if improvement_abs <= 0:
        return None
    if base_value is not None and abs(float(base_value)) > _BASE_EPS:
        return 100.0 * float(improvement_abs) / abs(float(base_value))
    # Near-zero base (e.g. Sharpe ~ 0): express absolute lift as percentage points.
    return 100.0 * float(improvement_abs)


def _format_performance_sentence(
    top: Sequence[tuple[str, str, float]],
) -> str:
    labels = [label for _, label, _ in top]
    pcts = [_format_pct(pct) for _, _, pct in top]
    preface = (
        "Performance statistics of the portfolio improved compared to the "
        "existing portfolio"
    )
    if len(labels) == 1:
        return f"{preface}, with {labels[0]} improving by {pcts[0]}."
    if len(labels) == 2:
        return (
            f"{preface}, with {labels[0]} and {labels[1]} improving by "
            f"{pcts[0]} and {pcts[1]} respectively."
        )
    name_join = ", ".join(labels[:-1]) + f", and {labels[-1]}"
    pct_join = ", ".join(pcts[:-1]) + f", and {pcts[-1]}"
    return f"{preface}, with {name_join} improving by {pct_join} respectively."


def _format_pct(pct: float) -> str:
    if pct >= 10:
        return f"{pct:.0f}%"
    if pct >= 1:
        return f"{pct:.1f}%"
    return f"{pct:.2f}%"


def _strategy_summary_bullet(sv: StrategyView | None) -> TalkingPoint | None:
    if sv is None:
        return None
    summary = sanitize_client_commentary((sv.summary or "").strip())
    if not summary:
        return None
    if summary.lower() in _SKIP_SUMMARY_MARKERS:
        return None
    if summary.lower().startswith("neutral stub"):
        return None
    return TalkingPoint(
        bullet=f"House view: {summary}",
        driver="strategy_summary",
    )


def sanitize_client_commentary(text: str) -> str:
    """Strip management-only knobs; rewrite tone tokens for client copy."""
    if not text or not str(text).strip():
        return ""
    out = str(text)

    # Drop coin-temperature / dial fragments (internal crypto sleeve knob).
    out = re.sub(
        r",?\s*coin[_\s-]?temperature\s*(?:=|:)?\s*\d+\s*/?\s*5?",
        "",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"coin[_\s-]?temperature\s*(?:=|:)?\s*\d+\s*/?\s*5?",
        "",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r",?\s*CRYPTO tilt scale\s*=\s*[0-9.]+(?:\s*;\s*3\s*=\s*neutral)?\.?",
        "",
        out,
        flags=re.IGNORECASE,
    )

    # tone=risk_off → "risk off tone" (client-facing phrasing).
    def _tone_sub(match: re.Match[str]) -> str:
        raw = match.group(1).lower().replace("-", "_")
        phrase = {
            "risk_off": "risk off tone",
            "riskoff": "risk off tone",
            "risk_on": "risk on tone",
            "riskon": "risk on tone",
            "neutral": "neutral tone",
        }.get(raw, f"{raw.replace('_', ' ')} tone")
        return phrase

    out = re.sub(
        r"\btone\s*=\s*(risk[_\s-]?off|risk[_\s-]?on|neutral)\b",
        _tone_sub,
        out,
        flags=re.IGNORECASE,
    )
    # Also catch bare "tone risk_off" without equals.
    out = re.sub(
        r"\btone\s+(risk[_\s-]?off|risk[_\s-]?on|neutral)\b",
        _tone_sub,
        out,
        flags=re.IGNORECASE,
    )

    # Tidy leftover punctuation from removals.
    out = re.sub(r"\(\s*,", "(", out)
    out = re.sub(r",\s*\)", ")", out)
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([.,;:])", r"\1", out)
    return out.strip(" ,;")


def _market_color_bullets(
    staged_trades: Sequence[StagedTrade | Mapping[str, Any]],
    strategy_view: StrategyView | None,
    *,
    opti_folio: Sequence[Mapping[str, Any] | Any],
    max_bullets: int,
) -> list[TalkingPoint]:
    """Color for top weight-move / high-weight names; trade-consistent only."""
    if strategy_view is None or max_bullets <= 0:
        return []

    views_by_asset = {
        str(v.asset).strip().upper(): v for v in strategy_view.asset_views
    }
    # Only the largest weight moves / holdings (2–3); do not backfill from tiny names.
    candidates = _rank_market_candidates(staged_trades, opti_folio)[:max_bullets]
    points: list[TalkingPoint] = []

    for cand in candidates:
        ticker = cand["ticker"]
        side = cand["side"]
        view = views_by_asset.get(ticker)
        if side == "BUY":
            if view is None or not _view_matches(view.final_view, _BULLISH_VIEW_TOKENS):
                continue
            points.append(
                TalkingPoint(
                    bullet=_format_buy_color(ticker, cand, view),
                    driver=f"market_color:{ticker}",
                )
            )
        elif side == "SELL":
            # Never promote a name we are selling, even if strategist was bullish.
            if view is not None and _view_matches(view.final_view, _BULLISH_VIEW_TOKENS):
                continue
            if view is not None and not _view_matches(
                view.final_view, _CAUTIOUS_VIEW_TOKENS
            ):
                continue
            points.append(
                TalkingPoint(
                    bullet=_format_sell_color(ticker, cand, view),
                    driver=f"market_color_underweight:{ticker}",
                )
            )
        elif side == "HOLD":
            if view is None or not _view_matches(view.final_view, _BULLISH_VIEW_TOKENS):
                continue
            points.append(
                TalkingPoint(
                    bullet=_format_hold_color(ticker, cand, view),
                    driver=f"market_color_hold:{ticker}",
                )
            )
    return points


def _rank_market_candidates(
    staged_trades: Sequence[StagedTrade | Mapping[str, Any]],
    opti_folio: Sequence[Mapping[str, Any] | Any],
) -> list[dict[str, Any]]:
    """Rank by max(|Δ weight|, ending weight) — largest moves or largest holdings."""
    by_ticker: dict[str, dict[str, Any]] = {}

    for trade in staged_trades:
        ticker = _trade_ticker(trade)
        if _is_cash_ticker(ticker):
            continue
        from_w = _trade_weight(trade, "from_weight_pct") or 0.0
        to_w = _trade_weight(trade, "to_weight_pct")
        if to_w is None:
            to_w = from_w
        side = _trade_side(trade)
        if side not in {"BUY", "SELL"}:
            continue
        score = _position_score(from_w, float(to_w))
        prev = by_ticker.get(ticker)
        if prev is None or score > float(prev["score"]):
            by_ticker[ticker] = {
                "ticker": ticker,
                "side": side,
                "from_weight_pct": from_w,
                "to_weight_pct": float(to_w),
                "score": score,
            }

    for pos in opti_folio:
        ticker, weight = _folio_ticker_weight(pos)
        if _is_cash_ticker(ticker) or weight is None:
            continue
        if ticker in by_ticker:
            # Already traded — keep trade side; bump score if holding weight larger.
            existing = by_ticker[ticker]
            existing["score"] = max(
                float(existing["score"]),
                _position_score(
                    float(existing.get("from_weight_pct") or 0.0),
                    float(existing.get("to_weight_pct") or weight),
                ),
                float(weight),
            )
            if existing.get("to_weight_pct") is None:
                existing["to_weight_pct"] = float(weight)
            continue
        by_ticker[ticker] = {
            "ticker": ticker,
            "side": "HOLD",
            "from_weight_pct": float(weight),
            "to_weight_pct": float(weight),
            "score": float(weight),
        }

    ranked = list(by_ticker.values())
    ranked.sort(key=lambda row: float(row["score"]), reverse=True)
    return ranked


def _position_score(from_w: float, to_w: float) -> float:
    return max(abs(float(to_w) - float(from_w)), abs(float(to_w)))


def _folio_ticker_weight(
    pos: Mapping[str, Any] | Any,
) -> tuple[str, float | None]:
    if isinstance(pos, Mapping):
        ticker = str(pos.get("ticker") or "").strip().upper()
        raw = pos.get("weight_pct")
    else:
        ticker = str(getattr(pos, "ticker", "") or "").strip().upper()
        raw = getattr(pos, "weight_pct", None)
    if raw is None:
        return ticker, None
    return ticker, float(raw)


def _format_buy_color(
    ticker: str,
    cand: Mapping[str, Any],
    view: StrategyAssetView,
) -> str:
    view_phrase = _humanize_view(view.final_view)
    from_w = cand.get("from_weight_pct")
    to_w = cand.get("to_weight_pct")
    weight_bit = ""
    if from_w is not None and to_w is not None:
        weight_bit = (
            f", with weight rising from {_pct_points(float(from_w))} to "
            f"{_pct_points(float(to_w))}"
        )
    elif to_w is not None and (from_w is None or float(from_w) <= 0):
        weight_bit = (
            f" and included at {_pct_points(float(to_w))} portfolio weight"
        )
    return (
        f"The outlook for {ticker} is {view_phrase}, so it was added or "
        f"increased in the holdings{weight_bit}."
    )


def _format_sell_color(
    ticker: str,
    cand: Mapping[str, Any],
    view: StrategyAssetView | None,
) -> str:
    from_w = cand.get("from_weight_pct")
    to_w = cand.get("to_weight_pct")
    weight_bit = ""
    if from_w is not None and to_w is not None:
        weight_bit = (
            f" (weight {_pct_points(float(from_w))} → {_pct_points(float(to_w))})"
        )

    if view is not None and _view_matches(view.final_view, _CAUTIOUS_VIEW_TOKENS):
        view_phrase = _humanize_view(view.final_view)
        return (
            f"{ticker} was underweighted{weight_bit} given a {view_phrase} "
            "outlook and risk considerations."
        )
    return (
        f"{ticker} was underweighted{weight_bit} to manage concentration / "
        "scenario risk in the optimized book."
    )


def _format_hold_color(
    ticker: str,
    cand: Mapping[str, Any],
    view: StrategyAssetView,
) -> str:
    view_phrase = _humanize_view(view.final_view)
    to_w = cand.get("to_weight_pct")
    weight_bit = (
        f" at {_pct_points(float(to_w))} portfolio weight"
        if to_w is not None
        else ""
    )
    return (
        f"The outlook for {ticker} is {view_phrase}; it remains a core "
        f"holding{weight_bit}."
    )


def _is_cash_ticker(ticker: str) -> bool:
    return not ticker or ticker in {"CASH", "CASH_USD", "USD", "HKD"}


def _view_matches(final_view: str, tokens: frozenset[str]) -> bool:
    norm = _normalize_view_token(final_view)
    if not norm:
        return False
    if norm in tokens:
        return True
    return any(tok in norm for tok in tokens)


def _normalize_view_token(raw: str) -> str:
    return (
        str(raw or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )


def _humanize_view(final_view: str) -> str:
    return _normalize_view_token(final_view).replace("_", " ")


def _pct_points(weight: float) -> str:
    """Format a portfolio weight as percent points (0.12 → 12%)."""
    # Weights in this codebase are typically fractions (0–1).
    pct = float(weight) * 100.0 if abs(float(weight)) <= 1.5 else float(weight)
    if abs(pct - round(pct)) < 0.05:
        return f"{pct:.0f}%"
    return f"{pct:.1f}%"


def _as_metrics_map(
    metrics: ClientPortfolioMetrics | Mapping[str, float | None] | None,
) -> dict[str, float | None]:
    if metrics is None:
        return {}
    if isinstance(metrics, ClientPortfolioMetrics):
        return metrics.model_dump()
    return {str(k): (None if v is None else float(v)) for k, v in metrics.items()}


def _as_strategy_view(
    raw: StrategyView | Mapping[str, Any] | None,
) -> StrategyView | None:
    if raw is None:
        return None
    if isinstance(raw, StrategyView):
        return raw
    return StrategyView.model_validate(raw)


def _trade_side(trade: StagedTrade | Mapping[str, Any]) -> str:
    if isinstance(trade, StagedTrade):
        return str(trade.side).upper()
    return str(trade.get("side") or "").upper()


def _trade_ticker(trade: StagedTrade | Mapping[str, Any]) -> str:
    if isinstance(trade, StagedTrade):
        return str(trade.ticker).strip().upper()
    return str(trade.get("ticker") or "").strip().upper()


def _trade_weight(
    trade: StagedTrade | Mapping[str, Any],
    field: str,
) -> float | None:
    if isinstance(trade, StagedTrade):
        val = getattr(trade, field, None)
    else:
        val = trade.get(field)
    if val is None:
        return None
    return float(val)


__all__ = [
    "build_talking_points",
    "extract_news_footnotes",
    "format_news_footnote_lines",
    "sanitize_client_commentary",
    "_talking_points_from_delta",
]
