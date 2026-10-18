"""Unit tests for deterministic scenario talking points."""

from __future__ import annotations

from backend.app.schemas.scenario import (
    ClientPortfolioMetrics,
    StagedTrade,
    StrategyAssetView,
    StrategyView,
)
from backend.app.services.scenario_talking_points import (
    build_talking_points,
    extract_news_footnotes,
    format_news_footnote_lines,
    _talking_points_from_delta,
)


def test_performance_sentence_improvements_only() -> None:
    points = build_talking_points(
        metrics_delta={
            "sharpe_ratio": 0.20,
            "max_runup": 0.05,
            "annualized_volatility": 0.02,  # worsened — must omit
            "max_drawdown": -0.01,  # improved (lower)
            "expected_return": -0.01,  # worsened — omit
            "sortino_ratio": None,
        },
        base_metrics=ClientPortfolioMetrics(
            sharpe_ratio=1.0,
            max_runup=0.25,
            annualized_volatility=0.20,
            max_drawdown=0.20,
            expected_return=0.10,
        ),
        staged_trades=[],
        strategy_view=None,
    )
    assert len(points) == 1
    bullet = points[0].bullet
    assert points[0].driver == "performance_summary"
    assert "Sharpe ratio" in bullet
    assert "max run-up" in bullet or "max drawdown" in bullet
    assert "annualized volatility" not in bullet.lower()
    assert "expected return" not in bullet.lower()
    assert "worsened" not in bullet.lower()
    assert "improved compared to the existing portfolio" in bullet


def test_talking_points_from_delta_compat_no_worsens() -> None:
    points = _talking_points_from_delta(
        {
            "expected_return": 0.02,
            "annualized_volatility": 0.01,  # worse
            "sharpe_ratio": 0.1,
        }
    )
    assert len(points) == 1
    assert "expected return" in points[0].bullet.lower()
    assert "sharpe" in points[0].bullet.lower()
    assert "volatility" not in points[0].bullet.lower()


def test_market_color_top_weight_moves_trade_gated() -> None:
    trades = [
        StagedTrade(
            order_id="ST-1",
            ticker="BTC-USD",
            asset_class="CRYPTO",
            side="BUY",
            quantity=1.0,
            notional_value=50_000.0,  # large notional must not override weight rank
            from_weight_pct=0.0,
            to_weight_pct=0.10,
        ),
        StagedTrade(
            order_id="ST-2",
            ticker="AAPL",
            asset_class="EQUITY",
            side="SELL",
            quantity=5.0,
            notional_value=2000.0,
            from_weight_pct=0.30,
            to_weight_pct=0.15,
        ),
        StagedTrade(
            order_id="ST-3",
            ticker="MSFT",
            asset_class="EQUITY",
            side="BUY",
            quantity=2.0,
            notional_value=1000.0,
            from_weight_pct=0.05,
            to_weight_pct=0.08,
        ),
        StagedTrade(
            order_id="ST-4",
            ticker="NVDA",
            asset_class="EQUITY",
            side="SELL",
            quantity=3.0,
            notional_value=1500.0,
            from_weight_pct=0.20,
            to_weight_pct=0.08,
        ),
        StagedTrade(
            order_id="ST-5",
            ticker="TINY",
            asset_class="EQUITY",
            side="BUY",
            quantity=1.0,
            notional_value=10.0,
            from_weight_pct=0.0,
            to_weight_pct=0.01,
        ),
    ]
    sv = StrategyView(
        source="house_rag",
        summary="Constructive on crypto; selective on mega-cap tech.",
        asset_views=[
            StrategyAssetView(
                asset="BTC-USD",
                final_view="moderately_positive",
                final_conviction=0.7,
                rationale="On-chain demand — must stay out of comment body.",
            ),
            StrategyAssetView(
                asset="AAPL",
                final_view="bullish",
                final_conviction=0.8,
                rationale="Must not appear — we are selling AAPL.",
            ),
            StrategyAssetView(
                asset="MSFT",
                final_view="cautious",
                final_conviction=0.6,
                rationale="Non-constructive buy — skip color.",
            ),
            StrategyAssetView(
                asset="NVDA",
                final_view="cautious",
                final_conviction=0.65,
                rationale="Tech concentration risk into the scenario horizon.",
            ),
            StrategyAssetView(
                asset="TINY",
                final_view="bullish",
                final_conviction=0.9,
                rationale="Small weight — should lose to larger moves.",
            ),
        ],
    )
    points = build_talking_points(
        metrics_delta={"sharpe_ratio": 0.1},
        base_metrics={"sharpe_ratio": 1.0},
        staged_trades=trades,
        strategy_view=sv,
        max_market_bullets=3,
    )
    market = [p for p in points if (p.driver or "").startswith("market_color")]
    drivers = [p.driver for p in market]
    text = " ".join(p.bullet for p in points)

    # Top weight scores among eligible: NVDA |Δw|=0.12, BTC to_w=0.10
    assert "market_color:BTC-USD" in drivers
    assert "market_color_underweight:NVDA" in drivers
    assert "market_color:TINY" not in drivers
    assert "market_color:AAPL" not in drivers
    assert "market_color:MSFT" not in drivers
    assert "AAPL" not in text
    # Market color present → canned house summary omitted
    assert all(p.driver != "strategy_summary" for p in points)
    assert "Consolidated strategy" not in text
    # News/rationale stays out of the talking-point body
    assert "On-chain demand" not in text
    assert "Public news" not in text
    assert len(market) <= 3


def test_news_footnotes_compact_publishers_by_symbol() -> None:
    articles = [
        {"symbol": "0700.HK", "title": "Long headline A", "publisher": "Zacks", "link": "https://a"},
        {"symbol": "0700.HK", "title": "Long headline B", "publisher": "Quartz", "link": "https://b"},
        {"symbol": "0700.HK", "title": "Long headline C", "publisher": "Moby", "link": "https://c"},
        {"symbol": "0700.HK", "title": "Long headline D", "publisher": "GuruFocus.com", "link": "https://d"},
        {"symbol": "0700.HK", "title": "Long headline E", "publisher": "Extra", "link": "https://e"},
        {"symbol": "AAPL", "title": "Apple news", "publisher": "Reuters", "link": "https://f"},
        {"symbol": "MSFT", "title": "MSFT news", "publisher": "Bloomberg", "link": "https://g"},
    ]
    footnotes = extract_news_footnotes(articles, limit=5, max_publishers_per_symbol=4)
    assert footnotes[0]["symbol"] == "0700.HK"
    assert footnotes[0]["publishers"] == [
        "Zacks",
        "Quartz",
        "Moby",
        "GuruFocus.com",
    ]
    assert "Extra" not in footnotes[0]["publishers"]
    lines = format_news_footnote_lines(footnotes)
    assert lines[0] == "[1] 0700.HK — Zacks, Quartz, Moby, GuruFocus.com"
    assert "https://" not in " ".join(lines)
    assert "Long headline" not in " ".join(lines)


def test_neutral_stub_summary_skipped() -> None:
    sv = StrategyView(
        source="stub",
        summary="Neutral stub strategy_view for Phase-1 UAT.",
        asset_views=[],
    )
    points = build_talking_points(
        metrics_delta={},
        staged_trades=[],
        strategy_view=sv,
    )
    assert all(p.driver != "strategy_summary" for p in points)


def test_sanitize_client_commentary_strips_management_knobs() -> None:
    from backend.app.services.scenario_talking_points import sanitize_client_commentary

    raw = (
        "Consolidated strategy_view: in-house RAG prioritized over public news "
        "(tone=risk_off, coin_temperature=2/5)."
    )
    clean = sanitize_client_commentary(raw)
    assert "coin_temperature" not in clean.lower()
    assert "tone=" not in clean.lower()
    assert "risk off tone" in clean.lower()

    sv = StrategyView(
        source="full",
        summary=raw,
        asset_views=[],
    )
    points = build_talking_points(
        metrics_delta={"sharpe_ratio": 0.1},
        base_metrics={"sharpe_ratio": 1.0},
        staged_trades=[],
        strategy_view=sv,
    )
    house = next(p for p in points if p.driver == "strategy_summary")
    assert "coin_temperature" not in house.bullet.lower()
    assert "tone=" not in house.bullet.lower()
    assert "risk off tone" in house.bullet.lower()

