"""Unit tests for WF-01 deterministic portfolio normalization."""

from __future__ import annotations

import pytest

from backend.app.schemas.portfolio_import import RawImportRow
from backend.app.services.portfolio_normalize import (
    ImportValidationError,
    normalize_import_rows,
)


def _row(
    *,
    alias: str | None = "Client_A",
    asset: str | None = "EQUITY",
    ticker: str | None = "AAPL",
    qty: float | None = 1.0,
    ccy: str | None = "USD",
) -> RawImportRow:
    return RawImportRow(
        client_alias=alias,
        asset_class=asset,
        ticker=ticker,
        quantity=qty,
        currency=ccy,
    )


def test_happy_path_normalization_and_cash_inject() -> None:
    rows = [
        _row(ticker="700", qty=1000, ccy="HKD"),
        _row(ticker="AAPL", qty=50, ccy="USD"),
        _row(asset="CRYPTO", ticker="BTC", qty=2.5, ccy="USD"),
        _row(asset="CASH", ticker="HKD", qty=50000, ccy="HKD"),
        _row(alias=None, ticker="9988", qty=500, ccy=None),
    ]
    result = normalize_import_rows(rows)
    tickers = {p.ticker: p for p in result.positions}
    assert "0700.HK" in tickers
    assert tickers["0700.HK"].currency == "HKD"
    assert "BTC-USD" in tickers
    assert tickers["BTC-USD"].asset_class == "CRYPTO"
    assert "9988.HK" in tickers
    assert any("derived" in m.lower() or "Ccy missing" in m for m in result.messages)
    # Already has HKD cash — no auto USD inject required for count, but USD may still inject
    assert 5 <= len(result.positions) <= 10


def test_count_after_cash_injection_bounds() -> None:
    rows = [_row(ticker=f"T{i}", qty=1.0) for i in range(3)]
    with pytest.raises(ImportValidationError, match="5 and 10"):
        normalize_import_rows(rows)


def test_multi_alias_rejected() -> None:
    rows = [
        _row(alias="A", ticker="AAPL"),
        _row(alias="B", ticker="MSFT"),
        _row(alias="A", ticker="JPM"),
        _row(alias="A", ticker="0700.HK", ccy="HKD"),
        _row(alias="A", ticker="NVDA"),
    ]
    with pytest.raises(ImportValidationError, match="Only 1 client"):
        normalize_import_rows(rows)


def test_negative_quantity_rejected() -> None:
    rows = [
        _row(ticker="AAPL", qty=-1),
        _row(ticker="MSFT"),
        _row(ticker="JPM"),
        _row(ticker="NVDA"),
        _row(ticker="0700.HK", ccy="HKD"),
    ]
    with pytest.raises(ImportValidationError, match="Negative"):
        normalize_import_rows(rows)


def test_currency_mismatch_ticker_wins() -> None:
    rows = [
        _row(ticker="AAPL", ccy="HKD"),
        _row(ticker="MSFT"),
        _row(ticker="JPM"),
        _row(ticker="NVDA"),
        _row(ticker="0700.HK", ccy="HKD"),
    ]
    result = normalize_import_rows(rows)
    aapl = next(p for p in result.positions if p.ticker == "AAPL")
    assert aapl.currency == "USD"
    assert aapl.currency_mismatch is True
    assert any("mismatch" in m.lower() for m in result.messages)


def test_zero_quantity_allowed() -> None:
    rows = [
        _row(ticker="AAPL", qty=0),
        _row(ticker="MSFT"),
        _row(ticker="JPM"),
        _row(ticker="NVDA"),
        _row(ticker="META"),
    ]
    result = normalize_import_rows(rows)
    assert any(p.ticker == "AAPL" and p.quantity == 0 for p in result.positions)
    assert any(p.asset_class == "CASH" and p.ticker == "USD" for p in result.positions)
