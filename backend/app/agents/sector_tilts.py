"""Deterministic Phase-1 sector tilts from free-text scenario prompts.

When the advisor names a sector (e.g. ``energy prices surge``), matching
tickers in the portfolio universe get a modest bullish/bearish view override
in the stub strategy_view — no LLM required.
"""

from __future__ import annotations

import re
from typing import Mapping

from backend.app.services.ticker_normalize import normalize_ticker


_SECTOR_BULL_CUES: tuple[str, ...] = (
    "surge",
    "rally",
    "soar",
    "boom",
    "bull",
    "rise",
    "rises",
    "rising",
    "jump",
    "jumps",
    "jumping",
    "double",
    "doubles",
    "upside",
    "spike",
    "spikes",
    "hike",
    "hikes",
    "rallying",
    "strength",
    "strong",
    "high prices",
    "price high",
    "prices high",
)

_SECTOR_BEAR_CUES: tuple[str, ...] = (
    "drop",
    "drops",
    "fall",
    "falls",
    "falling",
    "crash",
    "crashes",
    "plunge",
    "plunges",
    "selloff",
    "sell-off",
    "collapse",
    "decline",
    "declines",
    "down",
    "bear",
    "slump",
    "glut",
    "weak",
    "weakness",
    "low prices",
    "price low",
    "prices low",
)


class SectorTheme:
    """Cue keywords + representative liquid names for a thematic stub tilt."""

    __slots__ = ("key", "label", "cues", "tickers", "default_direction")

    def __init__(
        self,
        key: str,
        label: str,
        cues: tuple[str, ...],
        tickers: frozenset[str],
        *,
        default_direction: str | None = None,
    ) -> None:
        self.key = key
        self.label = label
        self.cues = cues
        self.tickers = tickers
        # If set, mentioning the theme without an explicit bull/bear verb
        # still applies a tilt (e.g. "oil shock" ⇒ higher prices ⇒ energy bull).
        self.default_direction = default_direction


SECTOR_THEMES: tuple[SectorTheme, ...] = (
    SectorTheme(
        "media",
        "media / streaming",
        (
            "media",
            "streaming",
            "netflix",
            "disney",
            "nflx",
            "comcast",
            "disney+",
            "entertainment",
        ),
        frozenset(
            {
                "NFLX",
                "DIS",
                "CMCSA",
                "PARA",
                "WBD",
                "FOXA",
                "FOX",
                "NWSA",
                "NWS",
                "SPOT",
                "ROKU",
                "LYV",
                "EA",
                "TTWO",
            }
        ),
    ),
    SectorTheme(
        "energy",
        "energy / oil & gas",
        (
            "energy",
            "oil",
            "brent",
            "wti",
            "crude",
            "opec",
            "gasoline",
            "natural gas",
            "natgas",
            "lng",
            "petroleum",
            "upstream",
            "refiner",
            "refiners",
        ),
        frozenset(
            {
                "XOM",
                "CVX",
                "COP",
                "EOG",
                "OXY",
                "SLB",
                "HAL",
                "BKR",
                "MPC",
                "VLO",
                "PSX",
                "WMB",
                "KMI",
                "OKE",
                "DVN",
                "PXD",
                "HES",
                "FANG",
                "BP",
                "SHEL",
                "TTE",
                "EQNR",
                "ENB",
                "TRP",
                "0857.HK",
                "0883.HK",
                "0386.HK",
            }
        ),
        # "oil shock" / "brent $120" imply higher prices → energy equities bullish
        default_direction="bull",
    ),
    SectorTheme(
        "tech",
        "technology / semiconductors",
        (
            "tech",
            "technology",
            "semiconductor",
            "semiconductors",
            "chip",
            "chips",
            "ai boom",
            "nasdaq",
            "software",
            "big tech",
            "faang",
            "magnificent 7",
            "mag7",
        ),
        frozenset(
            {
                "AAPL",
                "MSFT",
                "GOOGL",
                "GOOG",
                "AMZN",
                "META",
                "NVDA",
                "AVGO",
                "AMD",
                "INTC",
                "TSM",
                "ASML",
                "CRM",
                "ORCL",
                "ADBE",
                "CSCO",
                "QCOM",
                "TXN",
                "MU",
                "AMAT",
                "LRCX",
                "KLAC",
                "0700.HK",
                "9988.HK",
                "3690.HK",
            }
        ),
    ),
    SectorTheme(
        "banks",
        "banks / financials",
        (
            "bank",
            "banks",
            "banking",
            "financials",
            "financial sector",
            "lenders",
            "credit cycle",
            "net interest",
        ),
        frozenset(
            {
                "JPM",
                "BAC",
                "WFC",
                "C",
                "GS",
                "MS",
                "USB",
                "PNC",
                "TFC",
                "SCHW",
                "BLK",
                "BX",
                "KKR",
                "0005.HK",
                "0939.HK",
                "1398.HK",
                "3988.HK",
                "2318.HK",
            }
        ),
    ),
    SectorTheme(
        "healthcare",
        "healthcare / pharma",
        (
            "healthcare",
            "health care",
            "pharma",
            "pharmaceutical",
            "biotech",
            "biotechnology",
            "drug",
            "drugs",
            "hospital",
            "hospitals",
        ),
        frozenset(
            {
                "JNJ",
                "UNH",
                "LLY",
                "MRK",
                "ABBV",
                "PFE",
                "BMY",
                "AMGN",
                "GILD",
                "TMO",
                "DHR",
                "ISRG",
                "SYK",
                "MDT",
                "CVS",
                "CI",
                "ELV",
                "VRTX",
                "REGN",
            }
        ),
    ),
    SectorTheme(
        "consumer",
        "consumer / retail",
        (
            "consumer",
            "retail",
            "retailers",
            "discretionary",
            "staples",
            "ecommerce",
            "e-commerce",
            "shopping",
        ),
        frozenset(
            {
                "WMT",
                "COST",
                "TGT",
                "HD",
                "LOW",
                "NKE",
                "SBUX",
                "MCD",
                "PG",
                "KO",
                "PEP",
                "PM",
                "MO",
                "CL",
                "EL",
                "LULU",
                "TJX",
                "9992.HK",
                "2319.HK",
            }
        ),
    ),
    SectorTheme(
        "materials",
        "materials / mining",
        (
            "materials",
            "mining",
            "miners",
            "metals",
            "copper",
            "iron ore",
            "steel",
            "lithium",
            "commodity",
            "commodities",
            "gold miner",
            "gold miners",
        ),
        frozenset(
            {
                "FCX",
                "NEM",
                "GOLD",
                "SCCO",
                "BHP",
                "RIO",
                "VALE",
                "AA",
                "X",
                "NUE",
                "LIN",
                "APD",
                "SHW",
                "DD",
                "DOW",
                "ALB",
                "SQM",
                "2899.HK",
                "1088.HK",
                "1171.HK",
            }
        ),
    ),
    SectorTheme(
        "utilities",
        "utilities",
        (
            "utilities",
            "utility",
            "power producer",
            "electric utility",
        ),
        frozenset(
            {
                "NEE",
                "DUK",
                "SO",
                "D",
                "AEP",
                "EXC",
                "SRE",
                "PEG",
                "XEL",
                "ED",
                "WEC",
                "ES",
            }
        ),
    ),
    SectorTheme(
        "autos",
        "autos / EV",
        (
            "auto",
            "autos",
            "automotive",
            "electric vehicle",
            "electric vehicles",
            "car maker",
            "car makers",
            "ev sector",
            "ev stocks",
        ),
        frozenset(
            {
                "TSLA",
                "F",
                "GM",
                "RIVN",
                "LCID",
                "TM",
                "HMC",
                "STLA",
                "RACE",
                "1211.HK",
                "9866.HK",
                "2015.HK",
            }
        ),
    ),
    SectorTheme(
        "airlines",
        "airlines / travel",
        (
            "airline",
            "airlines",
            "aviation",
            "travel",
            "tourism",
            "hotel",
            "hotels",
            "cruise",
        ),
        frozenset(
            {
                "DAL",
                "UAL",
                "AAL",
                "LUV",
                "ALK",
                "BA",
                "MAR",
                "HLT",
                "H",
                "ABNB",
                "BKNG",
                "CCL",
                "RCL",
                "NCLH",
            }
        ),
    ),
    SectorTheme(
        "reits",
        "real estate / REITs",
        (
            "reit",
            "reits",
            "real estate",
            "property sector",
            "commercial property",
            "office vacancy",
        ),
        frozenset(
            {
                "PLD",
                "AMT",
                "EQIX",
                "CCI",
                "SPG",
                "O",
                "WELL",
                "DLR",
                "PSA",
                "AVB",
                "EQR",
                "VTR",
            }
        ),
    ),
    SectorTheme(
        "china_hk",
        "China / Hong Kong equities",
        (
            "china",
            "chinese",
            "hong kong",
            "hongkong",
            "h-share",
            "h shares",
            "hshares",
            "hang seng",
            "hangseng",
            "hsi",
            "csi 300",
            "csi300",
            "a-share",
            "a shares",
            "ashares",
            "tencent",
            "alibaba",
            "meituan",
            "xiaomi",
            "byd",
            "hk equity",
            "hk equities",
            "hk stock",
            "hk stocks",
            "china stock",
            "china stocks",
            "chinese stock",
            "chinese stocks",
        ),
        frozenset(
            {
                "0700.HK",
                "9988.HK",
                "3690.HK",
                "1810.HK",
                "9618.HK",
                "9888.HK",
                "1024.HK",
                "2318.HK",
                "0005.HK",
                "0939.HK",
                "1398.HK",
                "3988.HK",
                "0386.HK",
                "0857.HK",
                "0883.HK",
                "0941.HK",
                "1299.HK",
                "2269.HK",
                "1211.HK",
                "2015.HK",
                "9866.HK",
                "9868.HK",
                "9999.HK",
                "9626.HK",
                "6618.HK",
                "2382.HK",
                "2020.HK",
                "1177.HK",
                "1093.HK",
                "2688.HK",
            }
        ),
    ),
)

_SECTOR_CONVCTION = 0.72


def _cue_in_prompt(lower: str, cue: str) -> bool:
    """Substring match for multi-word / long cues; word-boundary for short tokens."""
    c = cue.strip().lower()
    if not c:
        return False
    if " " in c or len(c) >= 6:
        return c in lower
    return bool(re.search(rf"\b{re.escape(c)}\b", lower))


def _sector_direction_from_prompt(lower: str, theme: SectorTheme) -> str | None:
    """Bull/bear for one sector theme, or None if the theme is not cued."""
    if not any(_cue_in_prompt(lower, cue) for cue in theme.cues):
        return None
    bull = any(_cue_in_prompt(lower, k) for k in _SECTOR_BULL_CUES)
    bear = any(_cue_in_prompt(lower, k) for k in _SECTOR_BEAR_CUES)
    if bull and not bear:
        return "bull"
    if bear and not bull:
        return "bear"
    if bull and bear:
        # Mixed verbs near a sector cue — skip rather than guess wrong.
        return None
    return theme.default_direction


def detect_sector_tilts(user_prompt: str) -> dict[str, str]:
    """Return ``{sector_key: 'bull'|'bear'}`` for themes cued in the prompt."""
    lower = (user_prompt or "").lower()
    out: dict[str, str] = {}
    for theme in SECTOR_THEMES:
        direction = _sector_direction_from_prompt(lower, theme)
        if direction is not None:
            out[theme.key] = direction
    return out


def ticker_sector_tilt(
    ticker: str, tilts: Mapping[str, str]
) -> tuple[str, str, float] | None:
    """If ticker belongs to a tilted sector, return (sector_key, direction, conv)."""
    if not tilts:
        return None
    t = normalize_ticker(ticker)
    for theme in SECTOR_THEMES:
        direction = tilts.get(theme.key)
        if direction is None:
            continue
        theme_tickers = {normalize_ticker(x) for x in theme.tickers}
        if t in theme_tickers:
            return theme.key, direction, _SECTOR_CONVCTION
    return None


def sector_tilt_summary_bits(tilts: Mapping[str, str]) -> list[str]:
    """Human-readable fragments for strategy_view.summary."""
    bits: list[str] = []
    for theme in SECTOR_THEMES:
        direction = tilts.get(theme.key)
        if direction is None:
            continue
        side = "bullish" if direction == "bull" else "bearish"
        bits.append(f"{side} {theme.label}")
    return bits


def apply_sector_tilt_overrides(
    strategy_view: "StrategyView",
    user_prompt: str,
) -> "StrategyView":
    """
    Overlay deterministic sector tilts onto an existing strategy_view.

    Used for full (RAG/news) strategist output so prompts like
    ``Chinese stocks rally`` still boost matching HK names when research
    returns empty / tone-only defaults. Stub path already applies tilts
    inline; re-applying is idempotent.
    """
    from backend.app.schemas.scenario import StrategyView

    tilts = detect_sector_tilts(user_prompt)
    if not tilts:
        return strategy_view

    updated = []
    for av in strategy_view.asset_views:
        hit = ticker_sector_tilt(av.asset, tilts)
        if hit is None:
            updated.append(av)
            continue
        sector_key, direction, conv = hit
        view = "positive" if direction == "bull" else "cautious"
        rationale = (av.rationale or "").strip()
        override = f"{av.asset} {sector_key}-theme {direction} override"
        if override.lower() not in rationale.lower():
            rationale = f"{rationale} ({override}).".strip() if rationale else f"{override}."
        if hasattr(av, "model_copy"):
            updated.append(
                av.model_copy(
                    update={
                        "final_view": view,
                        "final_conviction": float(conv),
                        "rationale": rationale[:500],
                    }
                )
            )
        else:
            updated.append(av)

    notes = list(strategy_view.notes or [])
    bits = sector_tilt_summary_bits(tilts)
    note = (
        "sector_tilts="
        + ",".join(f"{k}:{v}" for k, v in sorted(tilts.items()))
        + "."
    )
    if note not in notes:
        notes.append(note)
    summary = strategy_view.summary or ""
    if bits and "Sector tilts:" not in summary:
        summary = f"{summary} Sector tilts: {'; '.join(bits)}.".strip()

    if isinstance(strategy_view, StrategyView) and hasattr(strategy_view, "model_copy"):
        return strategy_view.model_copy(
            update={"asset_views": updated, "notes": notes, "summary": summary}
        )
    return strategy_view


__all__ = [
    "SECTOR_THEMES",
    "SectorTheme",
    "apply_sector_tilt_overrides",
    "detect_sector_tilts",
    "sector_tilt_summary_bits",
    "ticker_sector_tilt",
]
