"""HTML table helper — never imports pyarrow (broken on many Anaconda installs)."""

from __future__ import annotations

import html
from typing import Any

import pandas as pd
import streamlit as st


def show_table(df: pd.DataFrame, *, max_rows: int = 50) -> None:
    """Render a pandas frame as escaped HTML (no Arrow / st.dataframe)."""
    if df is None or df.empty:
        st.caption("No rows.")
        return
    view = df.head(max_rows).copy()
    headers = "".join(f"<th>{html.escape(str(c))}</th>" for c in view.columns)
    body_rows: list[str] = []
    for _, row in view.iterrows():
        cells = "".join(f"<td>{_cell(v)}</td>" for v in row.tolist())
        body_rows.append(f"<tr>{cells}</tr>")
    table_html = (
        '<div style="overflow-x:auto;border:1px solid #E2E8F0;border-radius:8px;">'
        '<table style="width:100%;border-collapse:collapse;font-size:0.85rem;">'
        f"<thead><tr style=\"background:#F8F9FA;text-align:left;\">{headers}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table></div>"
    )
    _inject_html(table_html)


def _cell(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if hasattr(value, "isoformat"):
        try:
            return html.escape(value.isoformat(sep=" ", timespec="seconds"))
        except TypeError:
            return html.escape(str(value))
    return html.escape(str(value))


def _inject_html(markup: str) -> None:
    """Prefer ``st.html`` (no markdown code-fence leakage)."""
    if hasattr(st, "html"):
        st.html(markup)
    else:
        st.markdown(markup, unsafe_allow_html=True)
