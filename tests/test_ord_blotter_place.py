"""Ord blotter columns + Place All Orders action wiring (no DB)."""

from __future__ import annotations

from uuid import uuid4

from backend.app.schemas.scenario import StagedTrade
from backend.app.services.copilot_fsm import (
    ACTION_PLACE,
    ACTION_PROCESS,
    ORD_BLOTTER_SHEET,
    _orders_placed_action_ids,
    _overlay_excel_order_qty,
    build_ord_blotter_write,
)


def test_ord_blotter_columns_and_defaults() -> None:
    run_id = uuid4()
    trades = [
        StagedTrade(
            order_id="ST-1-AAPL-BUY-001",
            client_alias="demo_client",
            status="Pending",
            ticker="AAPL",
            asset_class="EQUITY",
            side="BUY",
            quantity=10,
            exec_type="Market",
            from_weight_pct=0.1,
            to_weight_pct=0.2,
        )
    ]
    spec = build_ord_blotter_write(trades, scenario_run_id=run_id)
    assert spec.sheet_name == ORD_BLOTTER_SHEET
    header = spec.values[0]
    assert header == [
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
    row = spec.values[1]
    assert row[0] == "ST-1-AAPL-BUY-001"
    assert row[1] == "demo_client"
    assert row[2] == "Pending"
    assert row[7] == "Market"
    assert row[6] == 10.0  # qty always 2dp float for Excel #,##0.00
    assert spec.column_number_formats.get("quantity") == "#,##0.00"
    assert spec.header_fill_colors.get("ticker") == "BDD7EE"
    assert spec.header_bold is True
    assert spec.table_border is True
    styles = {(s.column, s.equals.upper()): s for s in spec.value_font_styles}
    assert styles[("side", "BUY")].bold is True
    assert styles[("side", "BUY")].font_color == "006600"
    assert styles[("side", "SELL")].bold is True
    assert styles[("side", "SELL")].font_color == "C00000"
    assert all(str(c) != "scenario_run_id" for r in spec.values for c in r)


def test_place_action_id() -> None:
    assert ACTION_PLACE == "place_all_orders"


def test_orders_placed_restart_actions() -> None:
    from backend.app.services.copilot_fsm import ACTION_UPDATE_HOLDINGS

    assert _orders_placed_action_ids() == [
        ACTION_UPDATE_HOLDINGS,
        ACTION_PROCESS,
    ]


def test_overlay_excel_order_qty_preserves_advisor_edit() -> None:
    trades = [
        StagedTrade(
            order_id="ST-1-ETH-USD-BUY-005",
            client_alias="e2e_demo",
            status="Executed",
            ticker="ETH-USD",
            asset_class="CRYPTO",
            side="BUY",
            quantity=2.27,
            exec_type="Market",
        )
    ]
    excel = [
        {
            "order_id": "ST-1-ETH-USD-BUY-005",
            "ticker": "ETH-USD",
            "side": "BUY",
            "quantity": 5,
        }
    ]
    merged = _overlay_excel_order_qty(trades, excel)
    assert len(merged) == 1
    assert float(merged[0].quantity) == 5.0
    assert float(trades[0].quantity) == 2.27  # original unchanged
