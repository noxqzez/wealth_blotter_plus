"""Tests for strategist in-house priority merge."""

from __future__ import annotations

from backend.app.services.scenario_optimizer import qualitative_view_to_tilt
from backend.app.services.scenario_research import consolidate_strategy_view


def test_in_house_wins_on_conflict() -> None:
    house = {
        "hit_count": 1,
        "asset_views": [
            {
                "asset": "AAPL",
                "asset_class": "equity",
                "view": "cautious",
                "conviction": 0.7,
                "rationale": "Internal cautious on mega-cap.",
                "source_refs": [],
            }
        ],
    }
    market = {
        "article_count": 2,
        "asset_views": [
            {
                "asset": "AAPL",
                "asset_class": "equity",
                "view": "bullish",
                "conviction": 0.6,
                "rationale": "Street bullish headlines.",
                "source_refs": [],
            }
        ],
    }
    view = consolidate_strategy_view(
        house_research=house,
        market_research=market,
        universe_tickers=["AAPL", "BTC-USD"],
        coin_temperature=2,
        scenario_tone="risk_off",
    )
    aapl = next(v for v in view.asset_views if v.asset == "AAPL")
    assert aapl.final_view == "cautious"
    assert aapl.internal_view == "cautious"
    assert aapl.external_view == "bullish"
    assert aapl.final_conviction < 0.7
    assert "prioritized" in (aapl.conflict_resolution or "").lower()
    assert view.source == "full"
    assert "coin_temperature" not in (view.summary or "").lower()
    assert "tone=" not in (view.summary or "").lower()
    assert "risk off tone" in (view.summary or "").lower()


def test_mixed_market_falls_back_to_risk_on_tone() -> None:
    house = {"hit_count": 0, "asset_views": []}
    market = {
        "article_count": 10,
        "asset_views": [
            {
                "asset": "AAPL",
                "asset_class": "equity",
                "view": "mixed",
                "conviction": 0.5,
                "rationale": "Mixed headlines.",
                "source_refs": [],
            }
        ],
    }
    view = consolidate_strategy_view(
        house_research=house,
        market_research=market,
        universe_tickers=["AAPL"],
        coin_temperature=2,
        scenario_tone="risk_on",
    )
    aapl = next(v for v in view.asset_views if v.asset == "AAPL")
    assert aapl.final_view == "neutral_to_moderately_positive"
    assert qualitative_view_to_tilt(aapl.final_view) == 0.015
    assert "tone" in (aapl.conflict_resolution or "").lower()
