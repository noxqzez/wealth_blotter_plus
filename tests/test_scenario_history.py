"""Unit tests for scenario_history helpers (no network)."""

from __future__ import annotations

from backend.app.services.scenario_history import (
    build_scenario_universe,
    parse_lookback_days,
    yahoo_symbol_for,
)


def test_parse_lookback_days() -> None:
    assert parse_lookback_days("3Y") == 1095
    assert parse_lookback_days("1Y") == 365
    assert parse_lookback_days("6M") == 180
    assert parse_lookback_days("90D") == 90
    assert parse_lookback_days(None) == 1825  # default 5Y


def test_yahoo_symbol_mapping() -> None:
    assert yahoo_symbol_for("AAPL", "EQUITY") == "AAPL"
    assert yahoo_symbol_for("BTC", "CRYPTO") == "BTC-USD"
    assert yahoo_symbol_for("BTC-USD", "CRYPTO") == "BTC-USD"
    assert yahoo_symbol_for("CASH_USD", "CASH") is None
    # Eligibility is enforced by trading_rules upstream (universe builder).
    assert yahoo_symbol_for("SHIB", "CRYPTO") == "SHIB-USD"


def test_build_universe_adds_approved_coins() -> None:
    base = [
        {"ticker": "AAPL", "asset_class": "EQUITY"},
        {"ticker": "BTC-USD", "asset_class": "CRYPTO"},
        {"ticker": "CASH_USD", "asset_class": "CASH"},
    ]
    uni = build_scenario_universe(base, approved_crypto=["BTC", "ETH", "SOL"])
    tickers = {t for t, _ in uni}
    assert "AAPL" in tickers
    assert "BTC-USD" in tickers
    assert "ETH-USD" in tickers
    assert "SOL-USD" in tickers
    assert "CASH_USD" in tickers
    assert "DOGE-USD" not in tickers


def test_build_universe_drops_non_approved_base_crypto() -> None:
    base = [
        {"ticker": "AAPL", "asset_class": "EQUITY"},
        {"ticker": "SHIB-USD", "asset_class": "CRYPTO"},
    ]
    uni = build_scenario_universe(base, approved_crypto=["BTC"])
    tickers = {t for t, _ in uni}
    assert "SHIB-USD" not in tickers
    assert "BTC-USD" in tickers
