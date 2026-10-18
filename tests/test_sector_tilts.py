"""Deterministic sector theme detection for Phase-1 stub strategy_view."""

from __future__ import annotations

from backend.app.agents.scenario_crew import build_stub_strategy_view
from backend.app.agents.sector_tilts import detect_sector_tilts


def test_energy_prices_surge_tilts_energy_bull() -> None:
    tilts = detect_sector_tilts("energy prices surge")
    assert tilts == {"energy": "bull"}


def test_oil_shock_defaults_energy_bull() -> None:
    # No explicit surge/crash verb — oil shock still means higher prices.
    assert detect_sector_tilts("Geopolitical oil shock, Brent $120") == {
        "energy": "bull"
    }


def test_oil_crash_is_energy_bear() -> None:
    assert detect_sector_tilts("oil prices crash") == {"energy": "bear"}


def test_media_surge_still_works() -> None:
    assert detect_sector_tilts("next year media stocks surge") == {"media": "bull"}


def test_banks_rally() -> None:
    assert detect_sector_tilts("banks rally on rate cuts") == {"banks": "bull"}


def test_stub_applies_energy_override_under_risk_off() -> None:
    sv = build_stub_strategy_view(
        universe_tickers=["XOM", "AAPL", "NFLX"],
        scenario_tone="risk_off",
        user_prompt="energy prices surge",
    )
    by_ticker = {v.asset: v for v in sv.asset_views}
    assert by_ticker["XOM"].final_view == "positive"
    assert by_ticker["XOM"].final_conviction == 0.72
    # Non-energy names keep the global risk-off cautious default.
    assert by_ticker["AAPL"].final_view == "cautious"
    assert by_ticker["NFLX"].final_view == "cautious"


def test_no_sector_cue_no_tilt() -> None:
    assert detect_sector_tilts("mild seasonal volatility") == {}
