"""Fixtures for pilot golden-path E2E (no live Supabase / LLM / network)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.services.market_data import MarketQuote
from backend.app.services import copilot_session as sessions


DEMO_ADVISOR = UUID("00000000-0000-4000-8000-000000000001")


class _Chain:
    """Fluent mock for supabase ``table().select().eq()....execute()`` chains."""

    def __init__(self, store: dict[str, list[dict[str, Any]]], table: str) -> None:
        self._store = store
        self._table = table
        self._filters: dict[str, str] = {}
        self._op = "select"
        self._payload: Any = None

    def select(self, *_a: Any, **_k: Any) -> _Chain:
        self._op = "select"
        return self

    def insert(self, payload: Any, **_k: Any) -> _Chain:
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload: Any, **_k: Any) -> _Chain:
        self._op = "update"
        self._payload = payload
        return self

    def upsert(self, payload: Any, **_k: Any) -> _Chain:
        self._op = "upsert"
        self._payload = payload
        return self

    def delete(self, **_k: Any) -> _Chain:
        self._op = "delete"
        return self

    def eq(self, col: str, val: Any) -> _Chain:
        self._filters[col] = str(val)
        return self

    def ilike(self, col: str, val: Any) -> _Chain:
        self._filters[col] = str(val).lower()
        return self

    def limit(self, *_a: Any, **_k: Any) -> _Chain:
        return self

    def in_(self, *_a: Any, **_k: Any) -> _Chain:
        return self

    def execute(self) -> MagicMock:
        rows = self._store.setdefault(self._table, [])
        if self._op == "select":
            out = rows
            for k, v in self._filters.items():
                out = [r for r in out if str(r.get(k, "")).lower() == v.lower()]
            return MagicMock(data=list(out))
        if self._op == "insert":
            payload = self._payload
            items = payload if isinstance(payload, list) else [payload]
            created: list[dict[str, Any]] = []
            for item in items:
                row = dict(item)
                row.setdefault("id", str(uuid4()))
                rows.append(row)
                created.append(row)
            return MagicMock(data=created)
        if self._op == "update":
            updated: list[dict[str, Any]] = []
            for r in rows:
                if all(str(r.get(k, "")).lower() == v.lower() for k, v in self._filters.items()):
                    r.update(self._payload or {})
                    updated.append(r)
            return MagicMock(data=updated)
        if self._op == "upsert":
            payload = self._payload
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                rows.append(dict(item))
            return MagicMock(data=list(items))
        if self._op == "delete":
            keep: list[dict[str, Any]] = []
            deleted: list[dict[str, Any]] = []
            for r in rows:
                if all(str(r.get(k, "")).lower() == v.lower() for k, v in self._filters.items()):
                    deleted.append(r)
                else:
                    keep.append(r)
            self._store[self._table] = keep
            return MagicMock(data=deleted)
        return MagicMock(data=[])


class FakeSupabase:
    """Minimal in-memory stand-in for ``get_service_supabase()``."""

    def __init__(self) -> None:
        self.store: dict[str, list[dict[str, Any]]] = {
            "clients": [],
            "portfolios": [],
            "portfolio_positions": [],
            "audit_events": [],
            "usage_metrics": [],
            "scenario_runs": [],
            "profiles": [],
            "document_ingest_jobs": [],
        }

    def table(self, name: str) -> _Chain:
        return _Chain(self.store, name)


@pytest.fixture
def fake_supabase() -> FakeSupabase:
    return FakeSupabase()


@pytest.fixture
def e2e_api_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Authenticated via DEV_AUTH_BYPASS (pilot Excel local-style)."""
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    monkeypatch.setenv("DEV_ADVISOR_ID", str(DEMO_ADVISOR))
    monkeypatch.setenv("REQUIRE_BYOK", "false")
    monkeypatch.setenv("REQUIRE_PILOT_APPROVAL", "false")
    monkeypatch.setenv("ENVIRONMENT", "development")
    from backend.app.config import get_settings

    get_settings.cache_clear()
    from backend.app.main import app

    sessions.clear_all_sessions()
    with TestClient(app) as client:
        yield client
    sessions.clear_all_sessions()
    get_settings.cache_clear()


@pytest.fixture
def e2e_strict_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """No auth bypass — for gate tests (401/403/400)."""
    monkeypatch.setenv("DEV_AUTH_BYPASS", "false")
    monkeypatch.setenv("REQUIRE_BYOK", "true")
    monkeypatch.setenv("REQUIRE_PILOT_APPROVAL", "true")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-jwt-secret-not-for-prod-use")
    from backend.app.config import get_settings

    get_settings.cache_clear()
    from backend.app.main import app

    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


@pytest.fixture
def patch_supabase(
    monkeypatch: pytest.MonkeyPatch, fake_supabase: FakeSupabase
) -> FakeSupabase:
    """Route service-role client to in-memory fake."""
    monkeypatch.setattr(
        "backend.app.core.supabase.get_service_supabase",
        lambda: fake_supabase,
    )
    monkeypatch.setattr(
        "backend.app.services.portfolio_import.get_service_supabase",
        lambda: fake_supabase,
    )
    monkeypatch.setattr(
        "backend.app.services.usage_telemetry.get_service_supabase",
        lambda: fake_supabase,
        raising=False,
    )
    monkeypatch.setattr(
        "backend.app.services.audit_events.get_service_supabase",
        lambda: fake_supabase,
        raising=False,
    )
    return fake_supabase


@pytest.fixture
def patch_trading_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.app.services.portfolio_import.load_active_trading_rules",
        AsyncMock(return_value=[]),
    )


@pytest.fixture
def patch_market_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    def _quotes(tickers: list[str], *, limits: Any = None) -> dict[str, MarketQuote]:
        return {
            t: MarketQuote(spot_price=100.0, short_name=t, listing_status="FOUND")
            for t in tickers
        }

    monkeypatch.setattr(
        "backend.app.services.portfolio_import.fetch_market_quotes",
        _quotes,
    )
    monkeypatch.setattr(
        "backend.app.services.portfolio_import.fetch_hkd_per_usd",
        lambda *a, **k: 7.8,
    )


@pytest.fixture
def silence_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.app.api.v1.portfolios.write_application_event",
        AsyncMock(return_value=None),
    )


@pytest.fixture
def sample_import_rows() -> list[dict[str, Any]]:
    """5–10 securities (WF-01 technical limit)."""
    equities = ["AAPL", "MSFT", "GOOGL", "JPM", "XOM"]
    rows: list[dict[str, Any]] = [
        {
            "client_alias": "e2e_client",
            "asset_class": "EQUITY",
            "ticker": t,
            "quantity": 10.0 + i,
            "currency": "USD",
        }
        for i, t in enumerate(equities)
    ]
    rows.append(
        {
            "client_alias": "e2e_client",
            "asset_class": "CASH",
            "ticker": "USD",
            "quantity": 1000,
            "currency": "USD",
        }
    )
    return rows
