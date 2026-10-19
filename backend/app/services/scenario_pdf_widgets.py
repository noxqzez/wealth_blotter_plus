"""PDF flowables, performance chart, and card builders."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Flowable, Paragraph, Table, TableStyle

from backend.app.services.scenario_performance import top_holdings_for_slide
from backend.app.services.scenario_pdf_theme import (
    _CARD_BG,
    _CONTENT_WIDTH,
    _DONUT_COLORS,
    _GRID,
    _HIGHER_BETTER_STATS,
    _INK,
    _LINE_BASE,
    _LINE_OPTI,
    _LOWER_BETTER_STATS,
    _METRIC_ALIASES_IN_TEXT,
    _MUTED,
    _ORANGE,
    _PILL_BG,
    _STAT_DISPLAY,
    _cash_weight,
    _money,
    _pct,
    _portfolio_value,
    _ratio,
    _short_ticker,
)
from backend.app.services.scenario_talking_points import sanitize_client_commentary

class _EyebrowPill(Flowable):
    """Fixed grey pill matching the Gamma sample header chip."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text
        self._width = 0.0
        self._height = 16.0

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        del availHeight
        self._width = min(availWidth, 3.6 * inch)
        return self._width, self._height

    def draw(self) -> None:
        self.canv.setFillColor(_PILL_BG)
        self.canv.roundRect(0, 0, self._width, self._height, 7, fill=1, stroke=0)
        self.canv.setFillColor(_MUTED)
        self.canv.setFont("Helvetica", 7)
        self.canv.drawString(8, 5, self.text)


def _performance_chart(performance_series: Mapping[str, Any] | None) -> Drawing | None:
    if not isinstance(performance_series, Mapping):
        return None
    raw_points = performance_series.get("points") or []
    if not isinstance(raw_points, Sequence) or len(raw_points) < 2:
        return None

    base_pts: list[tuple[float, float]] = []
    opti_pts: list[tuple[float, float]] = []
    dates: list[str] = []
    for row in raw_points:
        if not isinstance(row, Mapping):
            continue
        try:
            b = float(row.get("starting_positions"))
            o = float(row.get("optimized_portfolio"))
        except (TypeError, ValueError):
            continue
        idx = float(len(base_pts))
        # Express as % change from 100 for Gamma-like axis labels
        base_pts.append((idx, (b / 100.0 - 1.0) * 100.0))
        opti_pts.append((idx, (o / 100.0 - 1.0) * 100.0))
        dates.append(str(row.get("date") or "").strip())
    if len(base_pts) < 2:
        return None

    width = float(_CONTENT_WIDTH)
    height = 200.0
    drawing = Drawing(width, height)

    # Chart background
    drawing.add(
        Rect(0, 0, width, height, fillColor=colors.white, strokeColor=None)
    )

    # Leave bottom margin for date labels so they never sit on the series.
    plot_bottom = 34.0
    plot_top_pad = 22.0
    lp = LinePlot()
    lp.x = 28
    lp.y = plot_bottom
    lp.height = height - plot_bottom - plot_top_pad
    lp.width = width - 70
    lp.data = [base_pts, opti_pts]
    lp.joinedLines = 1
    lp.lines[0].strokeColor = _LINE_BASE
    lp.lines[0].strokeWidth = 1.2
    lp.lines[1].strokeColor = _LINE_OPTI
    lp.lines[1].strokeWidth = 2.2
    x_max = max(p[0] for p in base_pts)
    lp.xValueAxis.valueMin = 0
    lp.xValueAxis.valueMax = x_max
    lp.xValueAxis.visible = 1
    lp.xValueAxis.visibleGrid = 0
    lp.xValueAxis.visibleAxis = 1
    lp.xValueAxis.strokeColor = _GRID
    lp.xValueAxis.strokeWidth = 0.5
    lp.xValueAxis.visibleTicks = 1
    lp.xValueAxis.tickUp = 0
    lp.xValueAxis.tickDown = 3
    lp.xValueAxis.labels.fontSize = 6.5
    lp.xValueAxis.labels.fillColor = _MUTED
    lp.xValueAxis.labels.boxAnchor = "n"
    lp.xValueAxis.labels.dy = -6
    lp.xValueAxis.valueSteps = _x_axis_tick_indices(len(base_pts))
    lp.xValueAxis.labelTextFormat = _make_x_date_formatter(dates)
    y_vals = [p[1] for p in base_pts] + [p[1] for p in opti_pts]
    y_min = min(y_vals)
    y_max = max(y_vals)
    pad = max(1.0, (y_max - y_min) * 0.12)
    lp.yValueAxis.valueMin = y_min - pad
    lp.yValueAxis.valueMax = y_max + pad
    lp.yValueAxis.labels.fontSize = 7
    lp.yValueAxis.labels.fillColor = _MUTED
    lp.yValueAxis.gridStrokeColor = _GRID
    lp.yValueAxis.gridStrokeWidth = 0.4
    lp.yValueAxis.visibleGrid = 1
    drawing.add(lp)

    label_base = str(performance_series.get("label_base") or "Starting Positions")
    label_opti = str(performance_series.get("label_opti") or "Optimized Portfolio")
    drawing.add(Line(28, height - 14, 44, height - 14, strokeColor=_LINE_BASE, strokeWidth=1.5))
    drawing.add(String(50, height - 17, f"{label_base} (%)", fontSize=8, fillColor=_INK))
    drawing.add(Line(200, height - 14, 216, height - 14, strokeColor=_LINE_OPTI, strokeWidth=2.2))
    drawing.add(String(222, height - 17, f"{label_opti} (%)", fontSize=8, fillColor=_INK))
    return drawing


def _x_axis_tick_indices(n_points: int) -> list[float]:
    """Evenly spaced x ticks including first and last sample."""
    if n_points <= 1:
        return [0.0]
    # 4 labels for short series, 5 for longer — keeps the axis readable.
    target = 5 if n_points >= 20 else 4
    target = min(target, n_points)
    if target <= 2:
        return [0.0, float(n_points - 1)]
    step = (n_points - 1) / (target - 1)
    return [float(round(i * step)) for i in range(target)]


def _make_x_date_formatter(dates: Sequence[str]):
    """Map LinePlot x-index → compact date label below the axis."""

    def _format(value: float) -> str:
        idx = int(round(float(value)))
        if idx < 0 or idx >= len(dates):
            return ""
        return _format_axis_date(dates[idx])

    return _format


def _format_axis_date(raw: str) -> str:
    """Compact bottom-axis date, e.g. ``Jan 2020`` or ``2020-01``."""
    text = (raw or "").strip()
    if not text:
        return ""
    # ISO date / datetime
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        year, month = text[:4], text[5:7]
        try:
            mon = int(month)
        except ValueError:
            return text[:10]
        names = (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        )
        if 1 <= mon <= 12:
            return f"{names[mon - 1]} {year}"
        return f"{year}-{month}"
    return text[:12]


# ---------------------------------------------------------------------------
# Cards (fixed equal height)
# ---------------------------------------------------------------------------

_CARD_HEIGHT = 2.72 * inch
_CARD_PAD_X = 9.0
_CARD_PAD_Y = 8.0
_CARD_TITLE_H = 16.0
_CARD_BODY_H = _CARD_HEIGHT - _CARD_TITLE_H - 2 * _CARD_PAD_Y


class _FixedCard(Flowable):
    """Identical outer box for every bottom card; body is clipped to the pad."""

    def __init__(
        self,
        title: str,
        body: Flowable,
        *,
        card_w: float,
        body_valign: str = "middle",
    ) -> None:
        super().__init__()
        self.title = title
        self.body = body
        self.card_w = float(card_w)
        self.body_valign = body_valign
        self.width = self.card_w
        self.height = float(_CARD_HEIGHT)

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        del availWidth, availHeight
        return self.width, self.height

    def draw(self) -> None:
        self.canv.setFillColor(_CARD_BG)
        self.canv.rect(0, 0, self.width, self.height, fill=1, stroke=0)
        self.canv.setFillColor(_ORANGE)
        self.canv.setFont("Helvetica-Bold", 9)
        self.canv.drawString(
            _CARD_PAD_X,
            self.height - _CARD_PAD_Y - 10,
            self.title,
        )
        # Clip body so nothing paints past the card edge.
        body_bottom = _CARD_PAD_Y
        body_top = self.height - _CARD_TITLE_H - _CARD_PAD_Y
        inner_w = self.card_w - 2 * _CARD_PAD_X
        body_h = max(0.0, body_top - body_bottom)
        self.canv.saveState()
        clip = self.canv.beginPath()
        clip.rect(_CARD_PAD_X, body_bottom, inner_w, body_h)
        self.canv.clipPath(clip, stroke=0)
        bw, bh = self.body.wrapOn(self.canv, inner_w, body_h)
        if self.body_valign == "top":
            y_off = body_bottom + max(0.0, body_h - bh)
        else:
            y_off = body_bottom + max(0.0, (body_h - bh) / 2.0)
        x_off = _CARD_PAD_X + max(0.0, (inner_w - bw) / 2.0)
        self.body.drawOn(self.canv, x_off, y_off)
        self.canv.restoreState()


class _StatsBlock(Flowable):
    """Hand-drawn stats rows — no table grid / lines, content-sized height."""

    def __init__(
        self,
        rows: Sequence[tuple[str, str, bool]],
        *,
        width: float,
        max_height: float,
    ) -> None:
        super().__init__()
        self.rows = list(rows)
        self.width = float(width)
        n = max(1, len(self.rows))
        # Fit rows inside max_height with a few points of bottom air.
        self.row_h = min(13.5, max(10.5, (float(max_height) - 6.0) / n))
        self.height = self.row_h * n

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        self.width = min(float(availWidth), self.width)
        self.height = min(self.height, float(availHeight))
        return self.width, self.height

    def draw(self) -> None:
        y = self.height - self.row_h
        for label, value, accent in self.rows:
            if y < -1:
                break
            baseline = y + max(2.0, self.row_h * 0.28)
            self.canv.setFillColor(_MUTED)
            self.canv.setFont("Helvetica", 7)
            self.canv.drawString(0, baseline, label)
            self.canv.setFillColor(_ORANGE if accent else _INK)
            self.canv.setFont("Helvetica-Bold", 7.5)
            self.canv.drawRightString(self.width, baseline, value)
            y -= self.row_h


class _CommentaryBlock(Flowable):
    """Wrapped bullets that auto-size to the largest font that fits the card."""

    _FONT_CANDIDATES: tuple[float, ...] = (9.5, 9.0, 8.5, 8.0, 7.5, 7.0, 6.5, 6.0, 5.5)

    def __init__(
        self,
        bullets: Sequence[str],
        *,
        width: float,
        max_height: float,
    ) -> None:
        super().__init__()
        self.bullets = [b.strip() for b in bullets if b and str(b).strip()][:4]
        self.width = float(width)
        self.max_height = float(max_height)
        self.height = self.max_height
        self.font_size = 7.0
        self.leading = 8.4
        self._gap = 2.5

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        self.width = min(float(availWidth), self.width)
        self.height = min(self.max_height, float(availHeight))
        self.font_size, self.leading = self._fit_font()
        return self.width, self.height

    def _fit_font(self) -> tuple[float, float]:
        for size in self._FONT_CANDIDATES:
            leading = size * 1.22
            if self._content_height(size, leading) <= self.height + 0.5:
                return size, leading
        size = self._FONT_CANDIDATES[-1]
        return size, size * 1.22

    def _content_height(self, font_size: float, leading: float) -> float:
        total = 0.0
        for i, raw in enumerate(self.bullets):
            lines = _wrap_text_width(f"• {raw}", self.width, font_size)
            total += leading * len(lines)
            if i < len(self.bullets) - 1:
                total += self._gap
        return total

    def draw(self) -> None:
        y = self.height - self.font_size
        for raw in self.bullets:
            lines = _wrap_text_width(f"• {raw}", self.width, self.font_size)
            block_h = self.leading * len(lines)
            if y - block_h + self.leading < -1:
                break
            for line in lines:
                if y < -1:
                    break
                if line.startswith("• ") and ":" in line:
                    head, rest = line.split(":", 1)
                    self.canv.setFillColor(_INK)
                    self.canv.setFont("Helvetica-Bold", self.font_size)
                    self.canv.drawString(0, y, head + ":")
                    head_w = self.canv.stringWidth(
                        head + ":", "Helvetica-Bold", self.font_size
                    )
                    self.canv.setFont("Helvetica", self.font_size)
                    self.canv.drawString(head_w, y, rest)
                else:
                    self.canv.setFillColor(_INK)
                    self.canv.setFont("Helvetica", self.font_size)
                    self.canv.drawString(0, y, line)
                y -= self.leading
            y -= self._gap


def _wrap_text_width(text: str, max_width: float, font_size: float) -> list[str]:
    """Word-wrap using Helvetica string widths at ``font_size``."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    cur = words[0]
    for word in words[1:]:
        trial = f"{cur} {word}"
        if stringWidth(trial, "Helvetica", font_size) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    return lines


class _DrawingBox(Flowable):
    """Adapter so a reportlab Drawing can live inside a card body."""

    def __init__(self, drawing: Drawing) -> None:
        super().__init__()
        self.drawing = drawing
        self.width = float(drawing.width)
        self.height = float(drawing.height)

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        del availWidth, availHeight
        return self.width, self.height

    def draw(self) -> None:
        self.drawing.drawOn(self.canv, 0, 0)

def _stats_card(
    opti: Mapping[str, Any],
    base: Mapping[str, Any],
    opti_folio: Sequence[Any],
    performance_series: Mapping[str, Any] | None,
    talking_points: Sequence[str],
    *,
    card_w: float,
) -> _FixedCard:
    rows_data = select_stats_rows(
        opti=opti,
        base=base,
        opti_folio=opti_folio,
        performance_series=performance_series,
        talking_points=talking_points,
    )
    inner_w = card_w - 2 * _CARD_PAD_X
    block = _StatsBlock(
        rows_data, width=inner_w, max_height=float(_CARD_BODY_H)
    )
    return _FixedCard(
        "Risk & Return Statistics", block, card_w=card_w, body_valign="top"
    )


def select_stats_rows(
    *,
    opti: Mapping[str, Any],
    base: Mapping[str, Any],
    opti_folio: Sequence[Any],
    performance_series: Mapping[str, Any] | None,
    talking_points: Sequence[str],
    max_rows: int = 6,
) -> list[tuple[str, str, bool]]:
    """Pick stats that match commentary + opti-vs-base improvements.

    Always leads with portfolio value and lookback return when available.
    Remaining slots prefer metrics named in commentary, then metrics where
    the optimized book is better (or less bad) than the starting book — the
    same improvement set the performance talking point is built from.
    """
    rows: list[tuple[str, str, bool]] = []
    used: set[str] = set()

    value = _portfolio_value(opti_folio)
    if value is not None and value > 0:
        rows.append(("Portfolio Value", _money(value), False))
        used.add("portfolio_value")

    lookback_opti = None
    lookback_base = None
    lookback_window = ""
    if isinstance(performance_series, Mapping):
        lookback_opti = performance_series.get("opti_total_return")
        lookback_base = performance_series.get("base_total_return")
        lookback_window = str(performance_series.get("lookback_window") or "").strip()
    if lookback_opti is not None:
        label = f"Lookback Return ({lookback_window})" if lookback_window else "Lookback Return"
        accent = False
        try:
            if lookback_base is not None and float(lookback_opti) > float(lookback_base):
                accent = True
        except (TypeError, ValueError):
            accent = True
        rows.append((label, _pct(lookback_opti), accent))
        used.add("lookback_return")

    mentioned = _metrics_mentioned_in_commentary(talking_points)
    improved = _improved_metric_keys(opti, base)

    # Commentary-mentioned first (intersection with available opti values),
    # preserving commentary order, then remaining improvements by lift.
    ordered_keys: list[str] = []
    for key in mentioned:
        if key not in ordered_keys and opti.get(key) is not None:
            ordered_keys.append(key)
    for key in improved:
        if key not in ordered_keys and opti.get(key) is not None:
            ordered_keys.append(key)

    # Fallback fill if still thin (show available opti metrics in a stable order).
    for key in _STAT_DISPLAY:
        if key not in ordered_keys and opti.get(key) is not None:
            ordered_keys.append(key)

    for key in ordered_keys:
        if len(rows) >= max_rows:
            break
        if key in used:
            continue
        display, fmt = _STAT_DISPLAY[key]
        raw = opti.get(key)
        formatted = _pct(raw) if fmt == "pct" else _ratio(raw)
        accent = key in improved
        rows.append((display, formatted, accent))
        used.add(key)

    return rows


def _metrics_mentioned_in_commentary(talking_points: Sequence[str]) -> list[str]:
    blob = " ".join(str(tp) for tp in talking_points).lower()
    found: list[str] = []
    for alias, key in _METRIC_ALIASES_IN_TEXT:
        if alias in blob and key not in found:
            found.append(key)
    return found


def _improved_metric_keys(
    opti: Mapping[str, Any],
    base: Mapping[str, Any],
) -> list[str]:
    """Metric keys where opti is better than base, ranked by relative lift."""
    ranked: list[tuple[str, float]] = []
    for key in _STAT_DISPLAY:
        o = opti.get(key)
        b = base.get(key)
        if o is None or b is None:
            continue
        try:
            ov = float(o)
            bv = float(b)
        except (TypeError, ValueError):
            continue
        if key in _HIGHER_BETTER_STATS:
            lift = ov - bv
            if lift <= 1e-12:
                continue
            pct = 100.0 * lift / abs(bv) if abs(bv) > 1e-9 else 100.0 * lift
        elif key in _LOWER_BETTER_STATS:
            # For drawdown/vol, more negative drawdown is worse; lower abs vol better.
            # max_drawdown is typically negative: improvement means ov > bv (e.g. -0.08 > -0.12).
            # annualized_volatility: lower is better => bv - ov > 0.
            if key == "max_drawdown":
                lift = ov - bv  # -0.08 - (-0.12) = +0.04 improvement
            else:
                lift = bv - ov
            if lift <= 1e-12:
                continue
            pct = 100.0 * lift / abs(bv) if abs(bv) > 1e-9 else 100.0 * lift
        else:
            continue
        ranked.append((key, pct))
    ranked.sort(key=lambda row: row[1], reverse=True)
    return [key for key, _ in ranked]


def _allocation_card(
    opti_folio: Sequence[Any],
    styles: Mapping[str, ParagraphStyle],
    *,
    card_w: float,
) -> _FixedCard:
    del styles
    holdings = top_holdings_for_slide(opti_folio, top_n=4)
    cash_w = _cash_weight(opti_folio)
    if cash_w > 1e-6:
        holdings = list(holdings) + [
            {"ticker": "Cash", "weight_pct": cash_w, "asset_class": "CASH"}
        ]

    inner_w = card_w - 2 * _CARD_PAD_X
    body_h = float(_CARD_BODY_H)
    # Square canvas so the donut sits dead-center in the card body.
    side = min(inner_w, body_h)
    drawing = Drawing(side, side)

    if not holdings:
        drawing.add(
            String(
                side / 2,
                side / 2,
                "No holdings available.",
                fontSize=7,
                fillColor=_MUTED,
                textAnchor="middle",
            )
        )
        return _FixedCard(
            "Holdings Allocation", _DrawingBox(drawing), card_w=card_w
        )

    weights = [max(0.0, float(h["weight_pct"])) for h in holdings]
    labels = [_short_ticker(str(h["ticker"])) for h in holdings]
    total = sum(weights) or 1.0
    weights = [w / total for w in weights]

    # Leave a small margin so labels on the ring stay inside the drawing.
    pie_w = side * 0.84
    pie_x = (side - pie_w) / 2.0
    pie_y = (side - pie_w) / 2.0

    pie = Pie()
    pie.x = pie_x
    pie.y = pie_y
    pie.width = pie_w
    pie.height = pie_w
    pie.data = weights
    pie.labels = None
    pie.simpleLabels = 0
    pie.sideLabels = 0
    pie.slices.strokeWidth = 0.8
    pie.slices.strokeColor = colors.white
    pie.innerRadiusFraction = 0.50
    for i in range(len(weights)):
        pie.slices[i].fillColor = _DONUT_COLORS[i % len(_DONUT_COLORS)]
        pie.slices[i].popout = 0
    drawing.add(pie)

    # Manual on-slice labels (centered on each arc mid-angle).
    # Always pair ticker + % when a label is shown (never bare "8%").
    cx = pie_x + pie_w / 2.0
    cy = pie_y + pie_w / 2.0
    r_mid = (pie_w / 2.0) * (pie.innerRadiusFraction + 1.0) / 2.0
    start_deg = float(pie.startAngle)
    for lab, w in zip(labels, weights, strict=False):
        sweep = w * 360.0
        mid_deg = start_deg - sweep / 2.0
        mid = math.radians(mid_deg)
        if w >= 0.06:
            tx = cx + r_mid * math.cos(mid)
            ty = cy + r_mid * math.sin(mid)
            short = lab if len(lab) <= 7 else lab[:7]
            text = f"{short} {w * 100:.0f}%"
            font_size = 5.5 if w >= 0.09 else 5.0
            drawing.add(
                String(
                    tx,
                    ty - 2.2,
                    text,
                    fontName="Helvetica-Bold",
                    fontSize=font_size,
                    fillColor=colors.white,
                    textAnchor="middle",
                )
            )
        start_deg -= sweep

    return _FixedCard("Holdings Allocation", _DrawingBox(drawing), card_w=card_w)


def _commentary_card(
    talking_points: Sequence[str],
    styles: Mapping[str, ParagraphStyle],
    *,
    card_w: float,
) -> _FixedCard:
    del styles
    bullets = [
        sanitize_client_commentary(tp)
        for tp in talking_points
        if tp and str(tp).strip()
    ]
    bullets = [b for b in bullets if b]
    inner_w = card_w - 2 * _CARD_PAD_X
    body = _CommentaryBlock(
        bullets,
        width=inner_w,
        max_height=float(_CARD_BODY_H),
    )
    return _FixedCard(
        "Advisor Commentary", body, card_w=card_w, body_valign="top"
    )
