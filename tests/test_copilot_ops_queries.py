"""Unit tests for Copilot ops-query parsing (no DB)."""

from __future__ import annotations

from uuid import uuid4

from backend.app.services.copilot_ops_queries import parse_ops_query


def test_parse_download_pdf_client() -> None:
    m = parse_ops_query("download client A x portfolio PDF")
    assert m is not None
    assert m.kind == "download_pdf"
    assert m.client_alias == "A"
    assert m.want_load is True


def test_parse_latest_portfolio_and_load() -> None:
    m = parse_ops_query("what is latest portfolio for client B — load it")
    assert m is not None
    assert m.kind == "latest_portfolio"
    assert m.client_alias == "B"
    assert m.want_load is True


def test_parse_optimized_portfolio_id() -> None:
    pid = uuid4()
    m = parse_ops_query(f"Show me optimized portfolio of portfolio ID {pid}")
    assert m is not None
    assert m.kind == "optimized_portfolio"
    assert m.portfolio_id == pid


def test_parse_update_prices() -> None:
    m = parse_ops_query("update prices for PortfolioID")
    assert m is not None
    assert m.kind == "update_prices"


def test_enrich_portfolio_is_not_ops_price_refresh() -> None:
    """Holdings restart phrases must not collide with update_prices ops."""
    assert parse_ops_query("enrich the portfolio selected") is None
    assert parse_ops_query("enrich portfolio") is None
    m = parse_ops_query("refresh prices for the portfolio")
    assert m is not None
    assert m.kind == "update_prices"


def test_parse_house_research() -> None:
    m = parse_ops_query("What did internal research have to say about this scenario")
    assert m is not None
    assert m.kind == "house_research"


def test_parse_latest_trades() -> None:
    m = parse_ops_query("What were the latest trades for client_alias Acme")
    assert m is not None
    assert m.kind == "latest_trades"
    assert m.client_alias == "Acme"


def test_parse_latest_trades_alias_before_client_word() -> None:
    m = parse_ops_query("what are the latest trades for e2e_demo client?")
    assert m is not None
    assert m.kind == "latest_trades"
    assert m.client_alias == "e2e_demo"


def test_parse_load_followup() -> None:
    m = parse_ops_query("load it")
    assert m is not None
    assert m.kind == "load_followup"


def test_parse_not_ops_is_none() -> None:
    assert parse_ops_query("Fed cuts rates 50bp risk on") is None
    assert parse_ops_query("change tone to risk_on") is None
