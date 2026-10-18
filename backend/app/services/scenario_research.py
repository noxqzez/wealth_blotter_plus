"""WF-02 house / market research + strategist consolidation (Phase-1 activation).

House: RAG retrieve by research_context_tags (deterministic synthesis from hits).
Market: Yahoo Finance public news headlines (broadly available; no cloud LLM required).
Strategist: pure Python merge — in-house view wins on conflict, lower conviction.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence
from uuid import UUID

import structlog

from backend.app.schemas.rules import TechnicalLimits
from backend.app.schemas.scenario import (
    AssetResearchView,
    CoinTemperatureLiteral,
    ResearchSourceRef,
    StrategyAssetView,
    StrategyView,
)
from backend.app.services.market_data import fetch_news
from backend.app.services.ticker_normalize import normalize_ticker as _normalize_ticker
from backend.app.services.rag_retrieve import retrieve_research_chunks
from backend.app.services.coin_temperature import (
    coin_temperature_scale,
    normalize_coin_temperature,
)

logger = structlog.get_logger(__name__)

_CASH = frozenset({"CASH", "CASH_USD", "USD", "HKD"})
# Substring tokens for crude headline sentiment (avoid bare "risk" — too noisy).
_BULLISH = frozenset(
    {
        "bullish",
        "positive",
        "upside",
        "outperform",
        "overweight",
        "strong",
        "resilient",
        "rally",
        "surge",
        "soar",
        "jump",
        "jumps",
        "record high",
        "all-time high",
        "all time high",
        "ath",
        "lift",
        "gain",
        "gains",
        "advance",
        "breakthrough",
        "optimism",
    }
)
_BEARISH = frozenset(
    {
        "bearish",
        "negative",
        "downside",
        "underperform",
        "underweight",
        "risk-off",
        "risk off",
        "cautious",
        "fail",
        "selloff",
        "sell-off",
        "plunge",
        "crash",
        "collapse",
        "slump",
        "weak",
        "fear",
        "recession",
        "layoff",
    }
)
_INDECISIVE_VIEWS = frozenset({"", "mixed", "neutral", "hold"})


def _tone_default_view(tone: str, *, is_crypto: bool) -> str:
    t = (tone or "neutral").strip().lower()
    if t == "risk_off":
        return "neutral_to_cautious" if is_crypto else "cautious"
    if t == "risk_on":
        return "neutral_to_moderately_positive"
    return "neutral"


def client_tone_phrase(tone: str) -> str:
    """Client-facing tone wording (never ``tone=risk_off`` style)."""
    t = (tone or "neutral").strip().lower().replace("-", "_").replace(" ", "_")
    return {
        "risk_off": "risk off tone",
        "riskoff": "risk off tone",
        "risk_on": "risk on tone",
        "riskon": "risk on tone",
        "neutral": "neutral tone",
    }.get(t, "neutral tone")


def _asset_class_for(ticker: str, fallback: str | None = None) -> str:
    t = _normalize_ticker(ticker)
    if t in _CASH:
        return "CASH"
    if t.endswith("-USD") or t in {"BTC", "ETH", "DOGE", "XRP", "SOL"}:
        return "CRYPTO"
    return (fallback or "EQUITY").upper()


def _view_from_text(text: str, *, default: str = "neutral") -> str:
    lower = text.lower()
    bull = sum(1 for w in _BULLISH if w in lower)
    bear = sum(1 for w in _BEARISH if w in lower)
    if bear > bull and bear > 0:
        return "cautious" if bear == 1 else "bearish"
    if bull > bear and bull > 0:
        return "neutral_to_moderately_positive" if bull == 1 else "bullish"
    return default


def _conviction(view: str, *, hit_count: int) -> float:
    base = 0.45 + min(0.35, 0.07 * hit_count)
    if "bearish" in view or "bullish" in view:
        return min(0.85, base + 0.1)
    if "cautious" in view or "positive" in view:
        return min(0.8, base + 0.05)
    return min(0.7, base)


async def run_house_research(
    *,
    advisor_id: UUID,
    scenario_params: Mapping[str, Any],
    universe_tickers: Sequence[str],
    top_k: int = 6,
) -> dict[str, Any]:
    """
    In-house research via RAG. Returns audit-friendly dict with asset_views.
    """
    tags = list(scenario_params.get("research_context_tags") or [])
    scenario = scenario_params.get("scenario") or {}
    query_bits = tags or [
        str(scenario.get("scenario_type") or ""),
        str(scenario.get("scenario_name") or ""),
        str(scenario_params.get("user_prompt") or "")[:200],
    ]
    query = " ".join(str(b) for b in query_bits if b).strip() or "market scenario outlook"
    messages: list[str] = []
    hits_payload: list[dict[str, Any]] = []

    try:
        result = await retrieve_research_chunks(
            advisor_id=advisor_id,
            query=query,
            top_k=top_k,
        )
        hits_payload = [
            {
                "content": h.content[:1200],
                "ticker": h.ticker,
                "asset_class": h.asset_class,
                "similarity": h.similarity,
                "original_filename": h.original_filename,
                "document_id": str(h.document_id),
                "chunk_index": h.chunk_index,
            }
            for h in result.hits
        ]
        messages.append(f"RAG retrieved {len(hits_payload)} chunk(s) for query.")
    except Exception as exc:  # noqa: BLE001 — soft-fail research
        messages.append(f"House RAG unavailable: {exc}")
        logger.warning("house_research_rag_failed", error=str(exc))

    # Group hits by ticker / asset class
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    general: list[dict[str, Any]] = []
    for hit in hits_payload:
        tk = _normalize_ticker(str(hit.get("ticker") or ""))
        if tk and tk not in _CASH:
            by_ticker.setdefault(tk, []).append(hit)
        else:
            general.append(hit)

    asset_views: list[dict[str, Any]] = []
    # Soft-fail / empty corpus: do not emit per-ticker "neutral" placeholders.
    # Empty asset_views lets the strategist fall through to market research.
    if not hits_payload:
        if not any("unavailable" in m.lower() or "rag" in m.lower() for m in messages):
            messages.append("No in-house RAG hits; deferring to market research.")
        return {
            "message_type": "in_house_research_view",
            "implementation": "rag",
            "query": query,
            "hit_count": 0,
            "hits": [],
            "asset_views": [],
            "summary_view": {
                "equities": "No in-house hits.",
                "crypto": "No in-house hits.",
            },
            "messages": messages,
            "research_context_tags": tags,
        }

    for raw in universe_tickers:
        ticker = _normalize_ticker(raw)
        if not ticker or ticker in _CASH:
            continue
        ac = _asset_class_for(ticker)
        matched = list(by_ticker.get(ticker, []))
        if not matched:
            # Broad asset-class hits
            matched = [
                h
                for h in general + [x for xs in by_ticker.values() for x in xs]
                if str(h.get("asset_class") or "").upper() in {ac, ac.title(), ""}
            ][:3]
        blob = " ".join(str(h.get("content") or "") for h in matched[:3])
        if not matched:
            continue
        view = _view_from_text(blob, default="neutral")
        conviction = _conviction(view, hit_count=len(matched))
        rationale = (
            f"In-house RAG ({len(matched)} hit(s)): "
            + (matched[0].get("content") or "")[:280]
        )
        refs = [
            ResearchSourceRef(
                document_id=str(h.get("document_id") or ""),
                section=str(h.get("original_filename") or "")[:120] or None,
                retrieval_score=float(h.get("similarity") or 0.0),
            ).model_dump(mode="json")
            for h in matched[:3]
        ]
        asset_views.append(
            AssetResearchView(
                asset=ticker,
                asset_class=ac.lower(),
                view=view,
                conviction=conviction,
                rationale=rationale,
                source_refs=[ResearchSourceRef.model_validate(r) for r in refs]
                if refs
                else [],
            ).model_dump(mode="json")
        )

    return {
        "message_type": "in_house_research_view",
        "implementation": "rag",
        "query": query,
        "hit_count": len(hits_payload),
        "hits": hits_payload[:12],
        "asset_views": asset_views,
        "summary_view": {
            "equities": "See per-asset views from internal RAG.",
            "crypto": "See per-asset views from internal RAG.",
        },
        "messages": messages,
        "research_context_tags": tags,
    }


def run_market_research(
    *,
    scenario_params: Mapping[str, Any],
    universe_tickers: Sequence[str],
    limits: TechnicalLimits | None = None,
) -> dict[str, Any]:
    """External research from Yahoo news headlines (no cloud LLM)."""
    tags = list(scenario_params.get("research_context_tags") or [])
    query = " ".join(tags) or str(scenario_params.get("user_prompt") or "markets")[:120]
    messages: list[str] = []
    try:
        articles = fetch_news(query, symbols=list(universe_tickers), limits=limits)
        messages.append(f"Fetched {len(articles)} public news headline(s) via Yahoo.")
    except Exception as exc:  # noqa: BLE001
        articles = []
        messages.append(f"Market news unavailable: {exc}")

    by_symbol: dict[str, list[dict[str, object]]] = {}
    for art in articles:
        sym = _normalize_ticker(str(art.get("symbol") or ""))
        if sym:
            by_symbol.setdefault(sym, []).append(art)

    asset_views: list[dict[str, Any]] = []
    for raw in universe_tickers:
        ticker = _normalize_ticker(raw)
        if not ticker or ticker in _CASH:
            continue
        matched = by_symbol.get(ticker, [])
        # Also apply general headlines if none for ticker
        blob_arts = matched or articles[:3]
        blob = " ".join(str(a.get("title") or "") for a in blob_arts)
        if blob_arts:
            view = _view_from_text(blob, default="mixed")
            conviction = _conviction(view, hit_count=len(blob_arts)) * 0.9
            rationale = f"Public news: " + "; ".join(
                str(a.get("title") or "")[:100] for a in blob_arts[:3]
            )
            refs = [
                ResearchSourceRef(
                    url=str(a.get("link") or "") or None,
                    publisher=str(a.get("publisher") or "") or None,
                ).model_dump(mode="json")
                for a in blob_arts[:3]
            ]
        else:
            view = "mixed"
            conviction = 0.35
            rationale = "No public headlines retrieved for this name."
            refs = []
        asset_views.append(
            AssetResearchView(
                asset=ticker,
                asset_class=_asset_class_for(ticker).lower(),
                view=view,
                conviction=float(min(0.75, conviction)),
                rationale=rationale,
                source_refs=[ResearchSourceRef.model_validate(r) for r in refs]
                if refs
                else [],
            ).model_dump(mode="json")
        )

    return {
        "message_type": "external_research_view",
        "implementation": "yahoo_news",
        "query": query,
        "article_count": len(articles),
        "articles": articles[:15],
        "asset_views": asset_views,
        "messages": messages,
        "research_context_tags": tags,
        "priority": "secondary",
    }


def _views_by_asset(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in payload.get("asset_views") or []:
        if isinstance(row, dict) and row.get("asset"):
            out[_normalize_ticker(str(row["asset"]))] = row
    return out


def consolidate_strategy_view(
    *,
    house_research: Mapping[str, Any],
    market_research: Mapping[str, Any],
    universe_tickers: Sequence[str],
    coin_temperature: CoinTemperatureLiteral | int | str = 2,
    scenario_tone: str = "neutral",
) -> StrategyView:
    """In-house wins on conflict; lower conviction when views disagree."""
    house = _views_by_asset(house_research)
    market = _views_by_asset(market_research)
    assets = []
    for raw in universe_tickers:
        t = _normalize_ticker(raw)
        if t and t not in _CASH:
            assets.append(t)
    for t in list(house) + list(market):
        if t not in assets and t not in _CASH:
            assets.append(t)

    tone = scenario_tone.strip().lower()
    default_eq = _tone_default_view(tone, is_crypto=False)
    default_crypto = _tone_default_view(tone, is_crypto=True)

    dial = normalize_coin_temperature(coin_temperature)
    temp_scale = coin_temperature_scale(dial)
    merged: list[StrategyAssetView] = []
    for ticker in assets:
        h = house.get(ticker)
        m = market.get(ticker)
        is_crypto = _asset_class_for(ticker) == "CRYPTO"
        tone_default = default_crypto if is_crypto else default_eq
        if h and m:
            hv = str(h.get("view") or tone_default)
            mv = str(m.get("view") or "mixed")
            if hv.lower() in _INDECISIVE_VIEWS:
                hv = tone_default
            if mv.lower() in _INDECISIVE_VIEWS:
                mv = tone_default
            hc = float(h.get("conviction") or 0.5)
            mc = float(m.get("conviction") or 0.4)
            conflict = hv.split("_")[0] != mv.split("_")[0] and hv != mv
            final_view = hv  # in-house priority
            final_conv = hc * (0.75 if conflict else 1.0) * (
                temp_scale if is_crypto else 1.0
            )
            resolution = (
                "Internal view prioritized over external sentiment."
                if conflict
                else "Internal and external views aligned; internal retained."
            )
            rationale = str(h.get("rationale") or "")[:400]
            if conflict:
                rationale = (
                    f"{rationale} External was '{mv}' (conviction {mc:.2f}); "
                    "in-house wins with reduced conviction."
                )
            merged.append(
                StrategyAssetView(
                    asset=ticker,
                    final_view=final_view,
                    final_conviction=float(min(0.85, max(0.25, final_conv))),
                    internal_view=hv,
                    external_view=mv,
                    conflict_resolution=resolution,
                    rationale=rationale[:500],
                )
            )
        elif h:
            hv = str(h.get("view") or tone_default)
            if hv.lower() in _INDECISIVE_VIEWS:
                hv = tone_default
            hc = float(h.get("conviction") or 0.5) * (temp_scale if is_crypto else 1.0)
            merged.append(
                StrategyAssetView(
                    asset=ticker,
                    final_view=hv,
                    final_conviction=float(min(0.85, hc)),
                    internal_view=hv,
                    external_view=None,
                    conflict_resolution="Internal only (no external view).",
                    rationale=str(h.get("rationale") or "")[:500],
                )
            )
        elif m:
            raw_mv = str(m.get("view") or "")
            mv = raw_mv or tone_default
            used_tone_fallback = mv.lower() in _INDECISIVE_VIEWS
            if used_tone_fallback:
                mv = tone_default
            mc = float(m.get("conviction") or 0.4) * 0.85 * (
                temp_scale if is_crypto else 1.0
            )
            merged.append(
                StrategyAssetView(
                    asset=ticker,
                    final_view=mv,
                    final_conviction=float(min(0.7, max(0.25, mc))),
                    internal_view=None,
                    external_view=raw_mv or mv,
                    conflict_resolution=(
                        "External indecisive; applied scenario-tone default."
                        if used_tone_fallback
                        else "External only (no in-house hit); used at lower conviction."
                    ),
                    rationale=str(m.get("rationale") or "")[:500],
                )
            )
        else:
            merged.append(
                StrategyAssetView(
                    asset=ticker,
                    final_view=tone_default,
                    final_conviction=0.45,
                    internal_view=None,
                    external_view=None,
                    conflict_resolution="No research hits; scenario-tone default.",
                    rationale=f"Default {tone_default} from scenario {client_tone_phrase(tone)}.",
                )
            )

    return StrategyView(
        source="full",
        asset_views=merged,
        summary=(
            "Consolidated strategy view prioritizing in-house research over "
            f"public news ({client_tone_phrase(tone)})."
        ),
        coin_temperature=dial,  # type: ignore[arg-type]
        notes=[
            "Strategist hierarchy enforced in code (not LLM).",
            f"coin_temperature dial={dial}/5 (CRYPTO tilt scale={temp_scale:.3f}; 3=neutral).",
            f"House hits={house_research.get('hit_count', 0)}; "
            f"market articles={market_research.get('article_count', 0)}.",
        ],
    )
