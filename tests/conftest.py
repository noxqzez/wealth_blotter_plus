"""Shared pytest fixtures for factory-backed tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from factories.portfolio_factory import PortfolioFactory


@pytest.fixture
def factory() -> PortfolioFactory:
    return PortfolioFactory(seed=42)


@pytest.fixture
def demo_bundle(factory: PortfolioFactory):
    return factory.make_demo_bundle(n_advisors=5, clients_per_advisor=3)


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """FastAPI TestClient with auth bypass; no live Supabase required for 4xx paths."""
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    monkeypatch.setenv(
        "DEV_ADVISOR_ID", "00000000-0000-4000-8000-000000000001"
    )
    # Clear cached settings so env takes effect.
    from backend.app.config import get_settings

    get_settings.cache_clear()
    from backend.app.main import app

    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


@pytest.fixture
def demo_advisor_id() -> UUID:
    return UUID("00000000-0000-4000-8000-000000000001")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live_llm: hits local Ollama/CrewAI (enabled when WBP_LIVE_LLM=1)",
    )
    config.addinivalue_line(
        "markers",
        "e2e: API/Copilot golden-path tests with mocked external I/O",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    # Default: skip live LLM. Enable with: WBP_LIVE_LLM=1 pytest ...
    if os.environ.get("WBP_LIVE_LLM", "").strip().lower() in {"1", "true", "yes"}:
        return
    skip = pytest.mark.skip(
        reason="Live LLM drift disabled — export WBP_LIVE_LLM=1 to enable"
    )
    for item in items:
        if "live_llm" in item.keywords:
            item.add_marker(skip)
