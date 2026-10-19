"""Deterministic sector theme detection for Phase-1 stub strategy_view."""

from __future__ import annotations

from backend.app.agents.scenario_crew import build_stub_strategy_view
from backend.app.agents.sector_tilts import (
    apply_sector_tilt_overrides,
    detect_sector_tilts,
    ticker_sector_tilt,
)
from backend.app.schemas.scenario import StrategyAssetView, StrategyView


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


def test_china_hk_rally() -> None:
    assert detect_sector_tilts("Chinese stocks rally") == {"china_hk": "bull"}
    assert detect_sector_tilts("Hong Kong equities surge") == {"china_hk": "bull"}


def test_hk_ticker_variants_match_china_theme() -> None:
    tilts = {"china_hk": "bull"}
    assert ticker_sector_tilt("0700.HK", tilts) is not None
    assert ticker_sector_tilt("700", tilts) is not None
    assert ticker_sector_tilt("700.HK", tilts) is not None
    assert ticker_sector_tilt("AAPL", tilts) is None


def test_stub_applies_china_hk_override() -> None:
    sv = build_stub_strategy_view(
        universe_tickers=["0700.HK", "AAPL"],
        scenario_tone="risk_off",
        user_prompt="Chinese stocks rally",
    )
    by_ticker = {v.asset: v for v in sv.asset_views}
    assert by_ticker["0700.HK"].final_view == "positive"
    assert by_ticker["0700.HK"].final_conviction == 0.72
    assert by_ticker["AAPL"].final_view == "cautious"


def test_apply_sector_tilt_overrides_on_tone_only_view() -> None:
    base = StrategyView(
        summary="Tone-only research.",
        asset_views=[
            StrategyAssetView(
                asset="0700.HK",
                final_view="cautious",
                final_conviction=0.5,
                rationale="tone default",
            )
        ],
        notes=[],
    )
    out = apply_sector_tilt_overrides(base, "Chinese stocks rally")
    assert out.asset_views[0].final_view == "positive"
    assert out.asset_views[0].final_conviction == 0.72


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
