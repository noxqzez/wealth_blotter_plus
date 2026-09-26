"""Unit tests for scenario_run pure helpers (no DB / network)."""

from __future__ import annotations

from uuid import uuid4

from backend.app.services.scenario_run import (
    _build_staged_trades,
    _normalize_ticker,
    _talking_points_from_delta,
)


def test_normalize_crypto_ticker() -> None:
    assert _normalize_ticker("btc") == "BTC-USD"
    assert _normalize_ticker("BTC-USD") == "BTC-USD"
    assert _normalize_ticker("AAPL") == "AAPL"


def test_talking_points_from_delta() -> None:
    points = _talking_points_from_delta(
        {"expected_return": 0.02, "annualized_volatility": -0.01, "sharpe_ratio": None}
    )
    drivers = {p.driver for p in points}
    assert "expected_return" in drivers
    assert "annualized_volatility" in drivers
    assert "sharpe_ratio" not in drivers


def test_staged_trades_buy_sell() -> None:
    run_id = uuid4()
    trades = _build_staged_trades(
        scenario_run_id=run_id,
        base_qty={"AAPL": 10.0, "BTC-USD": 0.0},
        base_weights={"AAPL": 1.0, "BTC-USD": 0.0},
        opti_positions=[
            {
                "ticker": "AAPL",
                "asset_class": "EQUITY",
                "quantity": 8.0,
                "weight_pct": 0.9,
                "notional_value": 900.0,
            },
            {
                "ticker": "BTC-USD",
                "asset_class": "CRYPTO",
                "quantity": 1.0,
                "weight_pct": 0.1,
                "notional_value": 100.0,
            },
        ],
        asset_classes={"AAPL": "EQUITY", "BTC-USD": "CRYPTO"},
    )
    by_ticker = {t.ticker: t for t in trades}
    assert by_ticker["AAPL"].side == "SELL"
    assert by_ticker["BTC-USD"].side == "BUY"
    assert by_ticker["BTC-USD"].order_id.startswith("ST-")
    assert by_ticker["BTC-USD"].quantity == 1.0
