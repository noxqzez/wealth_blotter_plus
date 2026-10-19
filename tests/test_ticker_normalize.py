"""Unit tests for shared ticker normalization."""

from __future__ import annotations

from backend.app.services.ticker_normalize import normalize_hk_code, normalize_ticker


def test_normalize_equity_and_crypto() -> None:
    assert normalize_ticker("aapl") == "AAPL"
    assert normalize_ticker("btc") == "BTC-USD"
    assert normalize_ticker("BTC-USD") == "BTC-USD"
    assert normalize_ticker("eth") == "ETH-USD"


def test_normalize_cash_aliases() -> None:
    assert normalize_ticker("USD") == "USD"
    assert normalize_ticker("CASH") == "CASH"
    assert normalize_ticker("CASH_USD") == "USD"
    assert normalize_ticker("hkd") == "HKD"


def test_normalize_empty() -> None:
    assert normalize_ticker("") == ""
    assert normalize_ticker("   ") == ""


def test_normalize_hk_variants_to_yahoo() -> None:
    """Map 700 / 700 HK / 700.HK → 0700.HK for news, RAG, and YFinance."""
    assert normalize_hk_code("700") == "0700.HK"
    assert normalize_hk_code("0700") == "0700.HK"
    assert normalize_hk_code("700.HK") == "0700.HK"
    assert normalize_hk_code("0700.HK") == "0700.HK"
    assert normalize_hk_code("700 HK") == "0700.HK"
    assert normalize_hk_code("700 hk") == "0700.HK"
    assert normalize_ticker("700") == "0700.HK"
    assert normalize_ticker("700.hk") == "0700.HK"
    assert normalize_ticker("9988.HK") == "9988.HK"
    assert normalize_ticker("5") == "0005.HK"
    assert normalize_hk_code("AAPL") is None
