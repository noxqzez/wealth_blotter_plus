"""Tests for Yahoo HKD→USD FX helpers and staged-trade qty consistency."""

from __future__ import annotations

from uuid import UUID

from backend.app.services.market_fx import (
    FALLBACK_HKD_PER_USD,
    currency_for_ticker,
    is_hk_equity_ticker,
    notional_usd,
    qty_from_usd_notional,
    to_usd,
)
from backend.app.services.scenario_optimizer import weights_to_opti_positions
from backend.app.services.scenario_run import _build_staged_trades


def test_hk_ticker_detection() -> None:
    assert is_hk_equity_ticker("0700.HK")
    assert is_hk_equity_ticker("9988.HK")
    assert not is_hk_equity_ticker("AAPL")
    assert currency_for_ticker("0700.HK") == "HKD"
    assert currency_for_ticker("AAPL") == "USD"


def test_to_usd_and_notional_keep_local_spot_separate() -> None:
    hkd_last = 434.2
    fx = 7.84324
    unit_usd = to_usd(hkd_last, "HKD", hkd_per_usd=fx)
    assert abs(unit_usd - (hkd_last / fx)) < 1e-9
    assert abs(notional_usd(100, hkd_last, "HKD", hkd_per_usd=fx) - 100 * unit_usd) < 1e-9
    assert to_usd(200.0, "USD", hkd_per_usd=fx) == 200.0


def test_0700_opti_qty_and_staged_with_local_hkd_last() -> None:
    """Last price stays HKD; FX only for USD notional → share qty.

    BASE: 100 sh @ 434.2 HKD, FX=7.84324 → notional USD ≈ 5535.98, w≈5.8564%
    OPTI: same book, w*≈10.183% → more USD notional → more shares → BUY.
    """
    fx = 7.84324
    hkd_last = 434.2
    base_qty = 100.0
    base_w = 0.058564
    base_notional = notional_usd(base_qty, hkd_last, "HKD", hkd_per_usd=fx)
    portfolio_value = base_notional / base_w
    opti_w = 0.10183051989872902
    opti_notional = portfolio_value * opti_w
    opti_qty = qty_from_usd_notional(opti_notional, hkd_last, "HKD", hkd_per_usd=fx)

    positions = weights_to_opti_positions(
        {"0700.HK": opti_w},
        asset_classes={"0700.HK": "EQUITY"},
        base_positions=[
            {
                "ticker": "0700.HK",
                "quantity": base_qty,
                "spot_price": hkd_last,
                "currency": "HKD",
                "notional_value": base_notional,
            }
        ],
        portfolio_value=portfolio_value,
        prices={"0700.HK": hkd_last},
        ticker_currency={"0700.HK": "HKD"},
        hkd_per_usd=fx,
    )
    assert len(positions) == 1
    p = positions[0]
    assert p["currency"] == "HKD"
    assert abs(float(p["spot_price"]) - hkd_last) < 1e-6
    assert abs(float(p["quantity"]) - int(opti_qty)) < 1e-9
    expected_notional = notional_usd(int(opti_qty), hkd_last, "HKD", hkd_per_usd=fx)
    assert abs(float(p["notional_value"]) - round(expected_notional, 2)) < 0.02

    run_id = UUID("3a9ec08c-8c57-4edd-9cd9-acd2cc14ab22")
    trades = _build_staged_trades(
        scenario_run_id=run_id,
        base_qty={"0700.HK": base_qty},
        base_weights={"0700.HK": base_w},
        opti_positions=positions,
        asset_classes={"0700.HK": "EQUITY"},
    )
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "BUY"
    assert t.order_id.startswith("ST-")
    assert t.quantity == float(int(opti_qty) - int(base_qty))
    assert FALLBACK_HKD_PER_USD == 7.8


def test_crypto_qty_two_decimals_and_staged_buy() -> None:
    """CRYPTO keeps fractional qty (2 dp); staged BUY appears from zero base."""
    spot = 84262.5
    weight = 0.17745
    portfolio_value = 126770.0  # ≈ scenario scale
    target_notional = portfolio_value * weight
    raw_qty = qty_from_usd_notional(target_notional, spot, "USD", hkd_per_usd=7.8)
    assert 0 < raw_qty < 1

    positions = weights_to_opti_positions(
        {"BTC-USD": weight},
        asset_classes={"BTC-USD": "CRYPTO"},
        portfolio_value=portfolio_value,
        prices={"BTC-USD": spot},
        ticker_currency={"BTC-USD": "USD"},
        hkd_per_usd=7.8,
    )
    assert len(positions) == 1
    p = positions[0]
    assert p["quantity"] == round(raw_qty, 2)
    assert p["quantity"] > 0
    assert abs(float(p["notional_value"]) - round(p["quantity"] * spot, 2)) < 0.02

    trades = _build_staged_trades(
        scenario_run_id=UUID("bf6b5b87-12e0-4328-99c6-3d6f7d6bb85f"),
        base_qty={},
        base_weights={},
        opti_positions=positions,
        asset_classes={"BTC-USD": "CRYPTO"},
    )
    assert len(trades) == 1
    assert trades[0].side == "BUY"
    assert trades[0].ticker == "BTC-USD"
    assert trades[0].quantity == p["quantity"]
    assert trades[0].order_id.startswith("ST-")
