"""Matplotlib chart helpers (avoid Streamlit Altair→pyarrow path).

Anaconda installs often ship a broken ``pyarrow`` (missing ``Table``, brotli
ABI mismatches). ``st.altair_chart`` / ``st.line_chart`` / ``st.bar_chart`` all
route through Arrow and crash. Matplotlib + ``st.image`` PNG buffers do not.
"""

from __future__ import annotations

import io

import pandas as pd
import streamlit as st

# Headless-safe backend before pyplot import.
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from frontend_streamlit.exec_monitor.theme import (
    BORDER,
    CHART_COLORS,
    GOLD,
    NAVY,
    NAVY_MID,
    SLATE,
)

_FONT = "Georgia"


def _style_axes(ax) -> None:
    ax.set_facecolor("none")
    for spine in ax.spines.values():
        spine.set_color(BORDER)
        spine.set_alpha(0.9)
    ax.tick_params(colors=SLATE, labelsize=8)
    ax.xaxis.label.set_color(SLATE)
    ax.yaxis.label.set_color(SLATE)
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_fontname(_FONT)
        label.set_color(SLATE)
    ax.grid(True, color=BORDER, alpha=0.0, linewidth=0.8)


def _style_legend(ax, *, loc: str = "upper left", outside: bool = False) -> None:
    kwargs: dict = {
        "loc": loc,
        "fontsize": 8,
        "frameon": False,
    }
    if outside:
        kwargs["bbox_to_anchor"] = (1.02, 1.0)
        kwargs["loc"] = "upper left"
        kwargs["borderaxespad"] = 0.0
    leg = ax.legend(**kwargs)
    _format_legend_texts(leg)


def _format_legend_texts(leg) -> None:
    if leg is None:
        return
    for text in leg.get_texts():
        text.set_fontname(_FONT)
        text.set_color(NAVY)
        text.set_fontsize(8)


def _legend_above(ax, *, n_items: int) -> None:
    """Centered transparent legend just above the axes (same as token area chart)."""
    leg = ax.legend(
        loc="lower center",
        bbox_to_anchor=(0.5, 1.02),
        ncol=max(1, n_items),
        fontsize=8,
        frameon=False,
    )
    _format_legend_texts(leg)


def _new_fig(figsize: tuple[float, float] = (7.5, 3.2)):
    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_alpha(0.0)
    _style_axes(ax)
    return fig, ax


def _show(fig, *, tight_rect: tuple[float, float, float, float] | None = None) -> None:
    """Render via PNG buffer — avoids deprecated ``st.pyplot(..., transparent=…)``."""
    if tight_rect is not None:
        fig.tight_layout(rect=tight_rect)
    else:
        fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True, dpi=200, bbox_inches="tight")
    buf.seek(0)
    st.image(buf)
    plt.close(fig)


def _millions_tick(value: float, _pos: int) -> str:
    if abs(value) < 1e-9:
        return "0"
    if float(value).is_integer():
        return f"{int(value)}M"
    return f"{value:g}M"


def show_line_wide(df: pd.DataFrame, *, x: str, title: str) -> None:
    """``df`` wide: ``x`` column + numeric series columns."""
    st.caption(title)
    if df is None or df.empty:
        st.info("No data.")
        return
    plot = df.copy()
    fig, ax = _new_fig()
    series_cols = [c for c in plot.columns if c != x]
    for i, col in enumerate(series_cols):
        ax.plot(
            plot[x],
            pd.to_numeric(plot[col], errors="coerce"),
            label=str(col),
            linewidth=1.8,
            color=CHART_COLORS[i % len(CHART_COLORS)],
        )
    _style_legend(ax)
    fig.autofmt_xdate()
    _show(fig)


def show_bar(df: pd.DataFrame, *, x: str, y: str, title: str) -> None:
    st.caption(title)
    if df is None or df.empty:
        st.info("No data.")
        return
    plot = df[[x, y]].copy()
    fig, ax = _new_fig()
    ax.bar(plot[x].astype(str), pd.to_numeric(plot[y], errors="coerce"), color=NAVY_MID)
    ax.tick_params(axis="x", labelrotation=25)
    _show(fig)


def show_area_wide(
    df: pd.DataFrame,
    *,
    x: str,
    title: str,
    y_in_millions: bool = False,
) -> None:
    st.caption(title)
    if df is None or df.empty:
        st.info("No data.")
        return
    plot = df.copy()
    series_cols = [c for c in plot.columns if c != x]
    scale = 1_000_000.0 if y_in_millions else 1.0
    ys = [
        (pd.to_numeric(plot[c], errors="coerce").fillna(0).to_numpy() / scale)
        for c in series_cols
    ]
    colors = [CHART_COLORS[i % len(CHART_COLORS)] for i in range(len(series_cols))]
    fig, ax = _new_fig()
    ax.stackplot(plot[x], *ys, labels=series_cols, colors=colors, alpha=0.88)
    _legend_above(ax, n_items=len(series_cols))
    if y_in_millions:
        ax.yaxis.set_major_formatter(FuncFormatter(_millions_tick))
    fig.autofmt_xdate()
    _show(fig, tight_rect=(0, 0, 1, 0.92))


def show_donut_as_bars(df: pd.DataFrame, *, category: str, value: str, title: str) -> None:
    """Spend breakdown as bars (no Arrow)."""
    st.caption(title)
    if df is None or df.empty:
        st.info("No data.")
        return
    plot = df[[category, value]].copy()
    fig, ax = _new_fig(figsize=(4.5, 3.0))
    ax.barh(
        plot[category].astype(str),
        pd.to_numeric(plot[value], errors="coerce"),
        color=[NAVY_MID, GOLD][: len(plot)],
    )
    _show(fig)


def show_grouped_bars(df: pd.DataFrame, *, title: str) -> None:
    """``df`` indexed by category with numeric status columns."""
    st.caption(title)
    if df is None or df.empty:
        st.info("No data.")
        return
    plot = df.copy()
    fig, ax = _new_fig()
    n = len(plot.columns)
    plot.plot(
        kind="bar",
        ax=ax,
        width=0.75,
        color=CHART_COLORS[:n],
    )
    ax.tick_params(axis="x", labelrotation=25)
    ax.set_xlabel("")
    _legend_above(ax, n_items=n)
    _show(fig, tight_rect=(0, 0, 1, 0.92))
