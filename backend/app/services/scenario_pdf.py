"""Deterministic client portfolio-review PDF (institutional confidential theme).

LAYOUT LOCKED (jpm-v1) — do not reshuffle the one-pager without an explicit
product decision. Fixed single-page composition so successive runs stay visually
identical in structure:

  eyebrow → title → chart subtitle → performance chart (+ x-axis dates)
  → bold scenario summary line → 3 equal cards (stats | donut | commentary)
  → footer / sources

No LLM. Chart series and donut weights come from the scenario run artifact
(``performance_series`` / ``opti_folio``); this module only renders them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Mapping, Sequence

from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from backend.app.schemas.scenario import ClientPortfolioMetrics
from backend.app.services.scenario_pdf_theme import (
    _CONTENT_WIDTH,
    _DISCLAIMER,
    _GENERIC_SCENARIO_NAMES,
    _INK,
    _MARGIN,
    _MUTED,
    _ORANGE,
    _PAGE,
    _as_metrics_map,
    _escape,
)
from backend.app.services.scenario_pdf_widgets import (
    _CARD_HEIGHT,
    _EyebrowPill,
    _allocation_card,
    _commentary_card,
    _performance_chart,
    _stats_card,
    select_stats_rows,
)

# Re-export for callers / tests that import from this module.
__all__ = [
    "build_scenario_pdf",
    "format_scenario_summary_line",
    "select_stats_rows",
]


def format_scenario_summary_line(scenario_name: str | None) -> str | None:
    """Bold callout under the chart: client-facing scenario description."""
    raw = (scenario_name or "").strip()
    if not raw:
        return None
    if raw.lower() in _GENERIC_SCENARIO_NAMES:
        return None
    # Already a full sentence.
    lower = raw.lower()
    if lower.startswith("scenario provided"):
        return raw if raw.endswith(".") else f"{raw}."
    if lower.startswith("scenario:"):
        body = raw.split(":", 1)[1].strip()
        if body:
            return f"Scenario provided was for {body}."
    # Trim trailing period then wrap.
    body = raw.rstrip(".")
    return f"Scenario provided was for {body}."


def build_scenario_pdf(
    *,
    scenario_name: str,
    opti_folio: list[Any],
    opti_metrics: ClientPortfolioMetrics | dict[str, Any],
    base_metrics: ClientPortfolioMetrics | dict[str, Any] | None,
    talking_points: list[str],
    client_alias: str | None = None,
    news_footnotes: Sequence[str] | None = None,
    performance_series: Mapping[str, Any] | None = None,
    as_of: datetime | None = None,
) -> bytes:
    """
    Render a fixed one-page private-banking style portfolio review.

    Layout (always the same):
      eyebrow → title → chart subtitle → performance chart → scenario summary
      → 3 equal cards (stats | allocation | commentary) → footer / sources
    """
    when = as_of or datetime.now(timezone.utc)
    alias = (client_alias or "Client").strip() or "Client"
    title = f"Portfolio Review: {alias} — {when.strftime('%B %d, %Y')}"

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=_PAGE,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=0.45 * inch,
        bottomMargin=0.4 * inch,
        title=title,
    )
    styles = _build_styles()
    story: list[Any] = []

    # --- Header ---
    story.append(_EyebrowPill("Not investment advice — CONFIDENTIAL"))
    story.append(Spacer(1, 0.14 * inch))
    story.append(Paragraph(_escape(title), styles["title"]))
    story.append(Spacer(1, 0.04 * inch))
    story.append(
        Paragraph("Portfolio Performance vs. Starting Positions", styles["subtitle"])
    )
    story.append(Spacer(1, 0.10 * inch))

    # --- Chart ---
    chart = _performance_chart(performance_series)
    if chart is not None:
        story.append(chart)
    else:
        story.append(
            Paragraph(
                "Performance chart unavailable for this run.",
                styles["muted"],
            )
        )
    story.append(Spacer(1, 0.08 * inch))

    scenario_line = format_scenario_summary_line(scenario_name)
    if scenario_line:
        story.append(Paragraph(_escape(scenario_line), styles["callout"]))
        story.append(Spacer(1, 0.12 * inch))
    else:
        story.append(Spacer(1, 0.08 * inch))

    # --- Three cards ---
    opti_map = _as_metrics_map(opti_metrics)
    base_map = _as_metrics_map(base_metrics) if base_metrics is not None else {}
    gap = 8.0
    card_w = (_CONTENT_WIDTH - 2 * gap) / 3.0

    stats_card = _stats_card(
        opti_map,
        base_map,
        opti_folio,
        performance_series,
        talking_points,
        card_w=card_w,
    )
    alloc_card = _allocation_card(opti_folio, styles, card_w=card_w)
    commentary_card = _commentary_card(talking_points, styles, card_w=card_w)

    cards = Table(
        [[stats_card, "", alloc_card, "", commentary_card]],
        colWidths=[card_w, gap, card_w, gap, card_w],
        rowHeights=[_CARD_HEIGHT],
    )
    cards.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ]
        )
    )
    story.append(cards)
    story.append(Spacer(1, 0.16 * inch))

    # --- Footer ---
    story.append(Paragraph(_escape(_DISCLAIMER), styles["footer"]))
    footnote_lines = [
        str(line).strip() for line in (news_footnotes or []) if str(line).strip()
    ]
    if footnote_lines:
        story.append(Spacer(1, 0.04 * inch))
        story.append(
            Paragraph(
                "Sources: " + _escape(" · ".join(footnote_lines[:5])),
                styles["footer"],
            )
        )

    doc.build(story)
    return buffer.getvalue()


def _build_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "JpmTitle",
            parent=base["Heading1"],
            fontName="Helvetica",
            fontSize=18,
            leading=22,
            textColor=_INK,
            alignment=TA_LEFT,
            spaceAfter=0,
        ),
        "subtitle": ParagraphStyle(
            "JpmSubtitle",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=11,
            leading=14,
            textColor=_ORANGE,
            alignment=TA_LEFT,
        ),
        "callout": ParagraphStyle(
            "JpmCallout",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=13,
            textColor=_ORANGE,
            alignment=TA_LEFT,
        ),
        "card_title": ParagraphStyle(
            "JpmCardTitle",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=12,
            textColor=_ORANGE,
            alignment=TA_LEFT,
        ),
        "stat_label": ParagraphStyle(
            "JpmStatLabel",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            textColor=_MUTED,
            alignment=TA_LEFT,
        ),
        "stat_value": ParagraphStyle(
            "JpmStatValue",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8.5,
            leading=10,
            textColor=_INK,
            alignment=TA_RIGHT,
        ),
        "stat_value_accent": ParagraphStyle(
            "JpmStatValueAccent",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8.5,
            leading=10,
            textColor=_ORANGE,
            alignment=TA_RIGHT,
        ),
        "legend": ParagraphStyle(
            "JpmLegend",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7.5,
            leading=9,
            textColor=_INK,
        ),
        "bullet": ParagraphStyle(
            "JpmBullet",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=6,
            leading=7.4,
            textColor=_INK,
            alignment=TA_LEFT,
        ),
        "muted": ParagraphStyle(
            "JpmMuted",
            parent=base["Normal"],
            fontName="Helvetica-Oblique",
            fontSize=8,
            leading=10,
            textColor=_MUTED,
        ),
        "footer": ParagraphStyle(
            "JpmFooter",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7,
            leading=9,
            textColor=_MUTED,
            alignment=TA_LEFT,
        ),
    }
