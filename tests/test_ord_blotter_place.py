"""Ord blotter columns + Place All Orders action wiring (no DB)."""

from __future__ import annotations

from uuid import uuid4

from backend.app.schemas.scenario import StagedTrade
from backend.app.services.copilot_fsm import (
    ACTION_PDF,
    ACTION_PLACE,
    ORD_BLOTTER_SHEET,
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
    assert all(str(c) != "scenario_run_id" for r in spec.values for c in r)


def test_place_and_pdf_action_ids() -> None:
    assert ACTION_PLACE == "place_all_orders"
    assert ACTION_PDF == "download_pdf"
