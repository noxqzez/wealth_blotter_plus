"""Locked PDF theme, metric labels, and small format helpers."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch

from backend.app.schemas.scenario import ClientPortfolioMetrics

# Bump only when deliberately changing the locked one-pager composition.
_LAYOUT_VERSION = "jpm-v1-locked"

_PAGE = letter
_MARGIN = 0.55 * inch

_INK = colors.HexColor("#1A1A1A")
_MUTED = colors.HexColor("#6B7280")
_ORANGE = colors.HexColor("#C4662A")
_CARD_BG = colors.HexColor("#E4ECF8")
_LINE_BASE = colors.HexColor("#111827")
_LINE_OPTI = colors.HexColor("#2563EB")
_GRID = colors.HexColor("#E5E7EB")
_PILL_BG = colors.HexColor("#F3F4F6")

_DONUT_COLORS: tuple[Any, ...] = (
    colors.HexColor("#0B1F33"),
    colors.HexColor("#1E3A5F"),
    colors.HexColor("#2F4B6E"),
    colors.HexColor("#3D5A80"),
    colors.HexColor("#4A6FA5"),
    colors.HexColor("#5C7CAA"),
    colors.HexColor("#6B8FBC"),
    colors.HexColor("#8FA4C8"),
    colors.HexColor("#A8B8D4"),
    colors.HexColor("#C5D0E0"),
    colors.HexColor("#9CA3AF"),
)

_DISCLAIMER = (
    "Disclaimer: Not investment advice. Scenario output is illustrative and "
    "may be incomplete or incorrect."
)

_CONTENT_WIDTH = _PAGE[0] - 2 * _MARGIN

_GENERIC_SCENARIO_NAMES: frozenset[str] = frozenset(
    {
        "market scenario",
        "loaded scenario",
        "scenario",
        "ignored",
        "x",
    }
)

_STAT_DISPLAY: dict[str, tuple[str, str]] = {
    "expected_return": ("Expected Return", "pct"),
    "annualized_volatility": ("Ann. Volatility", "pct"),
    "sharpe_ratio": ("Sharpe Ratio", "ratio"),
    "sortino_ratio": ("Sortino Ratio", "ratio"),
    "max_drawdown": ("Max Drawdown", "pct"),
    "max_runup": ("Max Run-up", "pct"),
    "cvar_95": ("CVaR 95%", "pct"),
    "scenario_return": ("Scenario Return", "pct"),
}
_HIGHER_BETTER_STATS: frozenset[str] = frozenset(
    {
        "expected_return",
        "sharpe_ratio",
        "sortino_ratio",
        "max_runup",
        "scenario_return",
    }
)
_LOWER_BETTER_STATS: frozenset[str] = frozenset(
    {
        "annualized_volatility",
        "max_drawdown",
        "cvar_95",
    }
)
_METRIC_ALIASES_IN_TEXT: tuple[tuple[str, str], ...] = (
    ("sharpe ratio", "sharpe_ratio"),
    ("sortino ratio", "sortino_ratio"),
    ("expected return", "expected_return"),
    ("annualized volatility", "annualized_volatility"),
    ("max drawdown", "max_drawdown"),
    ("max run-up", "max_runup"),
    ("max runup", "max_runup"),
    ("cvar 95%", "cvar_95"),
    ("cvar", "cvar_95"),
    ("scenario return", "scenario_return"),
)

def _short_ticker(ticker: str) -> str:
    t = ticker.strip()
    if t.upper() in {"OTHERS", "CASH"}:
        return t.title() if t.upper() == "CASH" else "Others"
    if t.endswith("-USD"):
        return t.removesuffix("-USD")
    if len(t) <= 7:
        return t
    return t[:7]

def _as_metrics_map(
    metrics: ClientPortfolioMetrics | Mapping[str, Any] | None,
) -> dict[str, Any]:
    if metrics is None:
        return {}
    if isinstance(metrics, ClientPortfolioMetrics):
        return metrics.model_dump()
    return dict(metrics)


def _portfolio_value(opti_folio: Sequence[Any]) -> float | None:
    total = 0.0
    found = False
    for pos in opti_folio:
        raw = pos.get("notional_value") if isinstance(pos, Mapping) else getattr(pos, "notional_value", None)
        if raw is None:
            continue
        total += float(raw)
        found = True
    return total if found else None


def _cash_weight(opti_folio: Sequence[Any]) -> float:
    total = 0.0
    for pos in opti_folio:
        if isinstance(pos, Mapping):
            ticker = str(pos.get("ticker") or "").strip().upper()
            ac = str(pos.get("asset_class") or "").strip().upper()
            w = pos.get("weight_pct")
        else:
            ticker = str(getattr(pos, "ticker", "") or "").strip().upper()
            ac = str(getattr(pos, "asset_class", "") or "").strip().upper()
            w = getattr(pos, "weight_pct", None)
        if w is None:
            continue
        if ac == "CASH" or ticker in {"CASH", "CASH_USD", "USD", "HKD"}:
            total += float(w)
    return total


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.0f}"


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{num * 100.0:.1f}%"


def _ratio(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
