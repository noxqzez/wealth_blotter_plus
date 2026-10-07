"""JPMorgan Private Bank aesthetic tokens for the exec monitor."""

from __future__ import annotations

BG = "#F3EEE8"
BG_SOFT = "#F8F9FA"
NAVY = "#002538"
NAVY_MID = "#004B87"
GOLD = "#C5A059"
SLATE = "#5F646A"
CARD = "#FFFFFF"
BORDER = "#E2E8F0"
SUCCESS = "#1F7A4D"
WARN = "#B45309"
DANGER = "#9B1C1C"
FONT = 'Georgia, "Times New Roman", serif'

CUSTOM_CSS = f"""
<style>
    .stApp {{
        background: linear-gradient(180deg, {BG} 0%, {BG_SOFT} 40%, #FFFFFF 100%);
        font-family: {FONT};
    }}
    [data-testid="stSidebar"] {{
        background-color: {CARD};
        border-right: 1px solid {BORDER};
    }}
    .wbp-title {{
        font-family: {FONT};
        color: {NAVY};
        font-size: 1.85rem;
        font-weight: 600;
        margin-bottom: 0.15rem;
        letter-spacing: -0.01em;
    }}
    .wbp-subtitle {{
        font-family: {FONT};
        color: {SLATE};
        font-size: 0.95rem;
        margin-bottom: 0.75rem;
    }}
    .wbp-section-title {{
        font-family: {FONT};
        color: {NAVY};
        font-size: 1.25rem;
        font-weight: 600;
        margin: 1.1rem 0 0.55rem 0;
        letter-spacing: -0.01em;
    }}
    .wbp-kpi {{
        background: {CARD};
        border: 1px solid {BORDER};
        border-radius: 8px;
        padding: 1rem 0.85rem;
        height: 128px;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        text-align: center;
        box-sizing: border-box;
    }}
    .wbp-kpi-label {{
        font-family: {FONT};
        color: {SLATE};
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        margin-bottom: 0.4rem;
        line-height: 1.2;
    }}
    .wbp-kpi-value {{
        font-family: {FONT};
        color: {NAVY};
        font-size: 1.45rem;
        font-weight: 650;
        line-height: 1.15;
    }}
    .wbp-kpi-hint {{
        font-family: {FONT};
        color: {SLATE};
        font-size: 0.75rem;
        margin-top: 0.4rem;
        line-height: 1.25;
    }}
    .wbp-badge {{
        display: inline-block;
        padding: 0.2rem 0.55rem;
        border-radius: 999px;
        font-family: {FONT};
        font-size: 0.72rem;
        font-weight: 600;
        margin: 0.15rem 0.35rem 0.15rem 0;
        border: 1px solid {BORDER};
        background: {CARD};
        color: {NAVY};
    }}
    .wbp-badge-ok {{ color: {SUCCESS}; border-color: #BBF7D0; background: #F0FDF4; }}
    .wbp-badge-warn {{ color: {WARN}; border-color: #FDE68A; background: #FFFBEB; }}
    /* Align Streamlit section headings with WBP serif headers */
    h2, h3, [data-testid="stMarkdownContainer"] h2,
    [data-testid="stMarkdownContainer"] h3 {{
        font-family: {FONT} !important;
        color: {NAVY} !important;
    }}
    div[data-testid="stMetricValue"] {{
        color: {NAVY};
        font-family: {FONT};
    }}
    .wbp-footnote {{
        font-family: {FONT};
        color: {SLATE};
        font-size: 0.7rem;
        line-height: 1.35;
        margin-top: 0.85rem;
        opacity: 0.85;
        border-top: 1px solid {BORDER};
        padding-top: 0.45rem;
    }}
</style>
"""

CHART_COLORS = [NAVY_MID, GOLD, SLATE, SUCCESS, "#6B8FBC", DANGER]


def inject_css() -> None:
    """Inject theme CSS without leaking raw ``<style>`` into the page body."""
    import streamlit as st

    if hasattr(st, "html"):
        st.html(CUSTOM_CSS)
    else:
        st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


def inject_html(markup: str) -> None:
    import streamlit as st

    if hasattr(st, "html"):
        st.html(markup)
    else:
        st.markdown(markup, unsafe_allow_html=True)


def section_title(text: str) -> None:
    inject_html(f'<div class="wbp-section-title">{text}</div>')
