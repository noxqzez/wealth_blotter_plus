"""Unit tests for scenario_run pure helpers (no DB / network)."""

from __future__ import annotations

from uuid import uuid4

from backend.app.services.scenario_run import _build_staged_trades
from backend.app.services.scenario_talking_points import _talking_points_from_delta
from backend.app.services.ticker_normalize import normalize_ticker


def test_normalize_crypto_ticker() -> None:
    assert normalize_ticker("btc") == "BTC-USD"
    assert normalize_ticker("BTC-USD") == "BTC-USD"
    assert normalize_ticker("AAPL") == "AAPL"


def test_talking_points_from_delta() -> None:
    points = _talking_points_from_delta(
        {"expected_return": 0.02, "annualized_volatility": -0.01, "sharpe_ratio": None}
    )
    assert len(points) == 1
    assert points[0].driver == "performance_summary"
    bullet = points[0].bullet.lower()
    assert "expected return" in bullet
    assert "annualized volatility" in bullet
    assert "sharpe" not in bullet
    assert "worsened" not in bullet


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
