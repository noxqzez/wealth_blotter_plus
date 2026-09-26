"""Stub strategy_view helpers."""

from __future__ import annotations

from backend.app.agents.scenario_crew import (
    build_stub_strategy_view,
    coin_temperature_from_config,
)


def test_stub_strategy_view_crypto_conviction_scales() -> None:
    cool = build_stub_strategy_view(
        universe_tickers=["AAPL", "BTC-USD"],
        coin_temperature=2,
        scenario_tone="neutral",
    )
    assert cool.coin_temperature == 2
    btc = next(v for v in cool.asset_views if v.asset == "BTC-USD")
    aapl = next(v for v in cool.asset_views if v.asset == "AAPL")
    # Dial 2 damps crypto conviction vs equities at same tone.
    assert btc.final_conviction <= aapl.final_conviction


def test_coin_temperature_from_config() -> None:
    assert coin_temperature_from_config({"coin_temperature": "hot"}) == 5
    assert coin_temperature_from_config({"coin_temperature": 2}) == 2
    assert coin_temperature_from_config({}) == 2
