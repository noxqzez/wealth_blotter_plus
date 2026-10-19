"""Copilot Excel write intents (scratchpad / rebalanced / ord_blotter)."""

from __future__ import annotations

from typing import Any, Sequence
from uuid import UUID

from backend.app.schemas.copilot import ExcelValueFontStyle, ExcelWriteSpec
from backend.app.schemas.portfolio_import import EnrichedPosition
from backend.app.schemas.scenario import ScenarioRunResponse

SCRATCHPAD_SHEET = "portfolio_scratchpad"
REBALANCED_SHEET = "rebalanced_portfolio"
ORD_BLOTTER_SHEET = "ord_blotter"

def _fmt_metric(n: float | None) -> float | str:
    if n is None:
        return ""
    return round(float(n), 4)


def _qty(n: float | None, asset_class: str | None) -> float | int | str:
    """Display qty always at 2dp so Excel #,##0.00 shows consistently."""
    if n is None:
        return ""
    _ = asset_class  # retained for call-site compatibility
    return round(float(n), 2)

# Light-blue header fill (matches enriched securities-list columns).
_HEADER_FILL_CYAN = "BDD7EE"


def _money(n: float | None) -> float | str:
    if n is None:
        return ""
    return round(float(n), 2)


def _pct(n: float | None) -> float | str:
    if n is None:
        return ""
    return round(float(n) * 100.0, 2)


def build_scratchpad_write(
    positions: list[EnrichedPosition],
    *,
    hide_source_sheet: str | None,
) -> ExcelWriteSpec:
    header = [
        "client_alias",
        "ticker",
        "asset_class",
        "currency",
        "quantity",
        "short_name",
        "last_price",
        "nominal_value",
        "Pct weight (USD)",
        "status",
        "price_timestamp",
        "pos_id",
    ]
    body: list[list[Any]] = []
    for p in positions:
        ts = p.price_timestamp.isoformat() if p.price_timestamp else ""
        body.append(
            [
                p.client_alias,
                p.ticker,
                p.asset_class,
                p.currency,
                # Always 2dp so Excel #,##0.00 shows "##.##" (not "##.").
                round(float(p.quantity), 2),
                p.short_name,
                round(float(p.spot_price), 2),
                round(float(p.market_value), 2) if p.market_value is not None else "",
                round(float(p.current_weight_pct) * 100.0, 2),
                p.status,
                ts,
                str(p.pos_id),
            ]
        )
    # Match advisor UX: gray = required paste fields; cyan = system enrichment.
    input_headers = (
        "client_alias",
        "ticker",
        "asset_class",
        "currency",
        "quantity",
    )
    enriched_headers = (
        "short_name",
        "last_price",
        "nominal_value",
        "Pct weight (USD)",
        "status",
        "price_timestamp",
        "pos_id",
    )
    header_fills = {
        **{h: "D9D9D9" for h in input_headers},
        **{h: "BDD7EE" for h in enriched_headers},
    }
    return ExcelWriteSpec(
        sheet_name=SCRATCHPAD_SHEET,
        values=[header, *body],
        activate=True,
        hide_source_sheet=hide_source_sheet
        if hide_source_sheet and hide_source_sheet != SCRATCHPAD_SHEET
        else None,
        column_number_formats={
            "quantity": "#,##0.00",
            "last_price": "#,##0.00",
            "nominal_value": "#,##0.00",
            "Pct weight (USD)": "0.00",
        },
        header_fill_colors=header_fills,
        header_bold=True,
        header_horizontal_alignment="center",
        table_border=True,
    )


def build_rebalanced_write(
    body: ScenarioRunResponse,
    *,
    scenario_run_id: UUID,
) -> ExcelWriteSpec:
    _ = scenario_run_id  # retained for call-site / audit correlation
    positions = body.opti_folio or []
    metric_keys = [
        "expected_return",
        "annualized_volatility",
        "sharpe_ratio",
        "sortino_ratio",
        "max_drawdown",
        "max_runup",
        "cvar_95",
        "scenario_return",
    ]
    metrics_header = ["metrics", "base", "opti", "delta"]
    rows: list[list[Any]] = [list(metrics_header)]
    base_m = body.base_client_metrics
    opti_m = body.opti_client_metrics
    delta = body.metrics_delta or {}
    for key in metric_keys:
        rows.append(
            [
                key,
                _fmt_metric(getattr(base_m, key, None) if base_m else None),
                _fmt_metric(getattr(opti_m, key, None) if opti_m else None),
                _fmt_metric(delta.get(key)),
            ]
        )
    rows.append([])
    rows.append(["opti_folio"])
    opti_header = [
        "ticker",
        "asset_class",
        "currency",
        "quantity",
        "nominal_value",
        "Pct weight (USD)",
    ]
    opti_header_row = len(rows)
    rows.append(list(opti_header))
    for p in positions:
        rows.append(
            [
                p.ticker,
                p.asset_class,
                p.currency or "",
                _qty(p.quantity, p.asset_class),
                _money(p.notional_value),
                _pct(p.weight_pct),
            ]
        )
    # Market commentary / talking points are PDF-only (simple + Gamma).

    width = max((len(r) for r in rows), default=1)
    padded = [r + [""] * (width - len(r)) for r in rows]
    header_fills = {
        **{h: _HEADER_FILL_CYAN for h in metrics_header},
        **{h: _HEADER_FILL_CYAN for h in opti_header},
    }
    return ExcelWriteSpec(
        sheet_name=REBALANCED_SHEET,
        values=padded,
        activate=True,
        column_number_formats={
            "base": "0.0000",
            "opti": "0.0000",
            "delta": "0.0000",
            "quantity": "#,##0.00",
            "nominal_value": "#,##0.00",
            "Pct weight (USD)": "0.00",
        },
        header_fill_colors=header_fills,
        header_row_indexes=[0, opti_header_row],
        header_bold=True,
        header_horizontal_alignment="center",
        table_border=True,
    )


def build_ord_blotter_write(
    body: ScenarioRunResponse | Sequence[Any],
    *,
    scenario_run_id: UUID,
) -> ExcelWriteSpec:
    _ = scenario_run_id  # retained for call-site / audit correlation
    if isinstance(body, ScenarioRunResponse):
        trades = list(body.staged_trades or [])
    else:
        trades = list(body)
    trades = sorted(
        trades,
        key=lambda t: (
            -(
                (t.to_weight_pct if getattr(t, "to_weight_pct", None) is not None else float("-inf"))
            ),
            str(t.ticker),
        ),
    )
    header = [
        "order_id",
        "client_alias",
        "Status",
        "ticker",
        "asset_class",
        "side",
        "quantity",
        "exec_type",
        "from_Pct weight (USD)",
        "to_Pct weight (USD)",
    ]
    rows: list[list[Any]] = [list(header)]
    for t in trades:
        rows.append(
            [
                t.order_id or "",
                getattr(t, "client_alias", None) or "",
                getattr(t, "status", None) or "Pending",
                t.ticker,
                t.asset_class,
                t.side,
                _qty(t.quantity, t.asset_class),
                getattr(t, "exec_type", None) or "Market",
                _pct(t.from_weight_pct),
                _pct(t.to_weight_pct),
            ]
        )
    width = max((len(r) for r in rows), default=1)
    padded = [r + [""] * (width - len(r)) for r in rows]
    return ExcelWriteSpec(
        sheet_name=ORD_BLOTTER_SHEET,
        values=padded,
        activate=False,
        column_number_formats={
            "quantity": "#,##0.00",
            "from_Pct weight (USD)": "0.00",
            "to_Pct weight (USD)": "0.00",
        },
        header_fill_colors={h: _HEADER_FILL_CYAN for h in header},
        header_row_indexes=[0],
        header_bold=True,
        header_horizontal_alignment="center",
        table_border=True,
        value_font_styles=[
            ExcelValueFontStyle(
                column="side", equals="BUY", bold=True, font_color="006600"
            ),
            ExcelValueFontStyle(
                column="side", equals="SELL", bold=True, font_color="C00000"
            ),
        ],
    )


def build_clear_sheet_write(sheet_name: str) -> ExcelWriteSpec:
    """Clear a sheet's used range (empty values); client leaves sheet visible."""
    return ExcelWriteSpec(
        sheet_name=sheet_name,
        values=[],
        activate=False,
    )


def build_stale_scenario_sheet_clears() -> list[ExcelWriteSpec]:
    """Clear prior opti output sheets when holdings restart abandons a scenario."""
    return [
        build_clear_sheet_write(REBALANCED_SHEET),
        build_clear_sheet_write(ORD_BLOTTER_SHEET),
    ]


# ---------------------------------------------------------------------------
