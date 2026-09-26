"""Deterministic scenario PDF render (WF-02) — reportlab only, no LLM."""

from __future__ import annotations

from io import BytesIO
from typing import Any, Mapping, Sequence

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from backend.app.schemas.scenario import ClientPortfolioMetrics

_DISCLAIMER = "Not investment advice"

_METRIC_ROWS: tuple[tuple[str, str], ...] = (
    ("expected_return", "Expected return"),
    ("annualized_volatility", "Annualized volatility"),
    ("sharpe_ratio", "Sharpe ratio"),
    ("sortino_ratio", "Sortino ratio"),
    ("max_drawdown", "Max drawdown"),
    ("max_runup", "Max run-up"),
    ("cvar_95", "CVaR 95%"),
    ("scenario_return", "Scenario return"),
)

_RATIO_KEYS: frozenset[str] = frozenset({"sharpe_ratio", "sortino_ratio"})

_HEADER_BG = colors.HexColor("#1F2937")
_HEADER_FG = colors.white
_ROW_ALT = colors.HexColor("#F3F4F6")
_GRID = colors.HexColor("#D1D5DB")
_MUTED = colors.HexColor("#6B7280")


def build_scenario_pdf(
    *,
    scenario_name: str,
    opti_folio: list[Any],
    opti_metrics: ClientPortfolioMetrics | dict[str, Any],
    base_metrics: ClientPortfolioMetrics | dict[str, Any] | None,
    talking_points: list[str],
    client_alias: str | None = None,
) -> bytes:
    """
    Render a corporate-simple scenario PDF.

    Layout: header → disclaimer → optimized folio table → optional base vs
    opti metrics → talking-point bullets. Deterministic; no LLM.
    """
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
        title=scenario_name.strip() or "Scenario report",
    )
    styles = _build_styles()
    story: list[Any] = []

    title = (scenario_name or "").strip() or "Scenario report"
    story.append(Paragraph(_escape(title), styles["title"]))
    if client_alias and client_alias.strip():
        story.append(
            Paragraph(
                f"Client: {_escape(client_alias.strip())}",
                styles["subtitle"],
            )
        )
    story.append(Spacer(1, 0.12 * inch))
    story.append(Paragraph(_escape(_DISCLAIMER), styles["disclaimer"]))
    story.append(Spacer(1, 0.28 * inch))

    story.append(Paragraph("Optimized portfolio", styles["section"]))
    story.append(Spacer(1, 0.08 * inch))
    story.append(_folio_table(opti_folio, styles))
    story.append(Spacer(1, 0.28 * inch))

    if base_metrics is not None:
        story.append(Paragraph("Metrics comparison", styles["section"]))
        story.append(Spacer(1, 0.08 * inch))
        story.append(
            _metrics_table(
                base=_as_metrics_map(base_metrics),
                opti=_as_metrics_map(opti_metrics),
                styles=styles,
            )
        )
        story.append(Spacer(1, 0.28 * inch))
    else:
        story.append(Paragraph("Optimized metrics", styles["section"]))
        story.append(Spacer(1, 0.08 * inch))
        story.append(
            _metrics_table(
                base=None,
                opti=_as_metrics_map(opti_metrics),
                styles=styles,
            )
        )
        story.append(Spacer(1, 0.28 * inch))

    story.append(Paragraph("Talking points", styles["section"]))
    story.append(Spacer(1, 0.08 * inch))
    story.append(_talking_points_block(talking_points, styles))

    doc.build(story)
    return buffer.getvalue()


def _build_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "WbpTitle",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=20,
            textColor=_HEADER_BG,
            alignment=TA_LEFT,
            spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "WbpSubtitle",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=10,
            leading=13,
            textColor=_MUTED,
            spaceAfter=2,
        ),
        "disclaimer": ParagraphStyle(
            "WbpDisclaimer",
            parent=base["Normal"],
            fontName="Helvetica-Oblique",
            fontSize=9,
            leading=11,
            textColor=_MUTED,
            alignment=TA_CENTER,
        ),
        "section": ParagraphStyle(
            "WbpSection",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14,
            textColor=_HEADER_BG,
            spaceBefore=0,
            spaceAfter=0,
        ),
        "cell": ParagraphStyle(
            "WbpCell",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=11,
        ),
        "cell_header": ParagraphStyle(
            "WbpCellHeader",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=11,
            textColor=_HEADER_FG,
        ),
        "bullet": ParagraphStyle(
            "WbpBullet",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
        ),
        "muted": ParagraphStyle(
            "WbpMuted",
            parent=base["Normal"],
            fontName="Helvetica-Oblique",
            fontSize=9,
            leading=11,
            textColor=_MUTED,
        ),
    }


def _folio_table(opti_folio: Sequence[Any], styles: Mapping[str, ParagraphStyle]) -> Table:
    header = [
        Paragraph("Ticker", styles["cell_header"]),
        Paragraph("Asset class", styles["cell_header"]),
        Paragraph("Weight %", styles["cell_header"]),
    ]
    rows: list[list[Any]] = [header]
    for pos in opti_folio:
        ticker, asset_class, weight_pct = _coerce_position(pos)
        rows.append(
            [
                Paragraph(_escape(ticker), styles["cell"]),
                Paragraph(_escape(asset_class), styles["cell"]),
                Paragraph(_format_weight_pct(weight_pct), styles["cell"]),
            ]
        )
    if len(rows) == 1:
        rows.append(
            [
                Paragraph("—", styles["cell"]),
                Paragraph("—", styles["cell"]),
                Paragraph("—", styles["cell"]),
            ]
        )

    table = Table(rows, colWidths=[2.2 * inch, 2.2 * inch, 1.6 * inch])
    table.setStyle(_table_style())
    return table


def _metrics_table(
    *,
    base: Mapping[str, Any] | None,
    opti: Mapping[str, Any],
    styles: Mapping[str, ParagraphStyle],
) -> Table:
    if base is not None:
        header = [
            Paragraph("Metric", styles["cell_header"]),
            Paragraph("Base", styles["cell_header"]),
            Paragraph("Optimized", styles["cell_header"]),
        ]
        col_widths = [2.4 * inch, 1.8 * inch, 1.8 * inch]
    else:
        header = [
            Paragraph("Metric", styles["cell_header"]),
            Paragraph("Optimized", styles["cell_header"]),
        ]
        col_widths = [3.0 * inch, 3.0 * inch]

    rows: list[list[Any]] = [header]
    for key, label in _METRIC_ROWS:
        opti_val = _format_metric(key, opti.get(key))
        if base is not None:
            rows.append(
                [
                    Paragraph(_escape(label), styles["cell"]),
                    Paragraph(_format_metric(key, base.get(key)), styles["cell"]),
                    Paragraph(opti_val, styles["cell"]),
                ]
            )
        else:
            rows.append(
                [
                    Paragraph(_escape(label), styles["cell"]),
                    Paragraph(opti_val, styles["cell"]),
                ]
            )

    table = Table(rows, colWidths=col_widths)
    table.setStyle(_table_style())
    return table


def _talking_points_block(
    talking_points: Sequence[str],
    styles: Mapping[str, ParagraphStyle],
) -> Any:
    bullets = [tp.strip() for tp in talking_points if tp and str(tp).strip()]
    if not bullets:
        return Paragraph("No talking points provided.", styles["muted"])

    items = [
        ListItem(Paragraph(_escape(text), styles["bullet"]), leftIndent=12, value="•")
        for text in bullets
    ]
    return ListFlowable(
        items,
        bulletType="bullet",
        start="•",
        leftIndent=18,
        bulletFontName="Helvetica",
        bulletFontSize=9,
    )


def _table_style() -> TableStyle:
    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
            ("TEXTCOLOR", (0, 0), (-1, 0), _HEADER_FG),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ALIGN", (0, 0), (-1, 0), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ROW_ALT]),
            ("GRID", (0, 0), (-1, -1), 0.4, _GRID),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
    )


def _as_metrics_map(
    metrics: ClientPortfolioMetrics | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(metrics, ClientPortfolioMetrics):
        return metrics.model_dump()
    return dict(metrics)


def _coerce_position(pos: Any) -> tuple[str, str, float | None]:
    if isinstance(pos, Mapping):
        ticker = str(pos.get("ticker") or "").strip() or "—"
        asset_class = str(pos.get("asset_class") or "").strip() or "—"
        raw_w = pos.get("weight_pct")
    else:
        ticker = str(getattr(pos, "ticker", "") or "").strip() or "—"
        asset_class = str(getattr(pos, "asset_class", "") or "").strip() or "—"
        raw_w = getattr(pos, "weight_pct", None)

    weight: float | None
    if raw_w is None:
        weight = None
    else:
        weight = float(raw_w)
    return ticker, asset_class, weight


def _format_weight_pct(weight_pct: float | None) -> str:
    """Display weight; ``weight_pct`` is a fraction in [0, 1] (optimizer convention)."""
    if weight_pct is None:
        return "—"
    return f"{weight_pct * 100.0:.2f}%"


def _format_metric(key: str, value: Any) -> str:
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return _escape(str(value))

    if key in _RATIO_KEYS:
        return f"{num:.2f}"
    # Returns / vol / drawdown / CVaR: fraction → percent
    return f"{num * 100.0:.2f}%"


def _escape(text: str) -> str:
    """Minimal XML escape for reportlab Paragraph markup."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
