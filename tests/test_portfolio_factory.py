"""Determinism / shape checks for the shared data factory."""

from __future__ import annotations

from factories.portfolio_factory import PortfolioFactory


def test_seed_42_is_deterministic() -> None:
    a = PortfolioFactory(seed=42).make_demo_bundle()
    b = PortfolioFactory(seed=42).make_demo_bundle()
    assert a.to_summary() == b.to_summary()
    assert [c.client_alias for c in a.clients] == [c.client_alias for c in b.clients]
    assert [str(p.id) for p in a.portfolios] == [str(p.id) for p in b.portfolios]
    assert [
        (pos.ticker, pos.quantity) for p in a.portfolios for pos in p.positions
    ] == [(pos.ticker, pos.quantity) for p in b.portfolios for pos in p.positions]


def test_demo_counts_match_brief() -> None:
    bundle = PortfolioFactory(seed=42).make_demo_bundle(
        n_advisors=5, clients_per_advisor=3
    )
    assert len(bundle.advisors) == 5
    assert len(bundle.clients) == 15
    assert len(bundle.portfolios) == 15
    for folio in bundle.portfolios:
        assert len(folio.positions) >= 5
        w = sum(p.current_weight_pct for p in folio.positions)
        assert 0.99 <= w <= 1.01
