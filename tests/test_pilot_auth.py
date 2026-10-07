"""Phase 1 pilot auth / BYOK unit tests (no live Supabase required)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app.core.byok import (
    ByokCredentials,
    clear_byok,
    redact_headers,
    require_byok_for_llm,
    set_byok,
)
from backend.app.services.pilot_auth import require_approved_profile


def test_redact_headers_strips_llm_and_auth() -> None:
    out = redact_headers(
        {
            "Authorization": "Bearer secret",
            "X-LLM-API-Key": "sk-live-secret",
            "Content-Type": "application/json",
        }
    )
    assert out["Authorization"] == "[REDACTED]"
    assert out["X-LLM-API-Key"] == "[REDACTED]"
    assert out["Content-Type"] == "application/json"


def test_require_byok_in_production() -> None:
    clear_byok()
    settings = MagicMock()
    settings.environment = "production"
    settings.require_byok = True
    with pytest.raises(HTTPException) as exc:
        require_byok_for_llm(settings=settings)
    assert exc.value.status_code == 400


def test_require_byok_satisfied_from_context() -> None:
    set_byok(ByokCredentials(api_key="sk-test", provider="openai"))
    settings = MagicMock()
    settings.environment = "production"
    settings.require_byok = True
    creds = require_byok_for_llm(settings=settings)
    assert creds.api_key == "sk-test"
    clear_byok()


def test_pending_profile_forbidden() -> None:
    aid = uuid4()
    with patch(
        "backend.app.services.pilot_auth.fetch_profile",
        return_value={
            "id": str(aid),
            "email": "a@b.com",
            "role": "advisor",
            "approval_status": "PENDING_APPROVAL",
        },
    ):
        with pytest.raises(HTTPException) as exc:
            require_approved_profile(aid)
        assert exc.value.status_code == 403
        detail = exc.value.detail
        assert isinstance(detail, dict)
        assert detail["error_code"] == "WBP-AUTH-PENDING"


def test_approved_profile_ok() -> None:
    aid = uuid4()
    with patch(
        "backend.app.services.pilot_auth.fetch_profile",
        return_value={
            "id": str(aid),
            "email": "a@b.com",
            "role": "advisor",
            "approval_status": "APPROVED",
        },
    ):
        profile = require_approved_profile(aid)
        assert profile["approval_status"] == "APPROVED"


def test_register_endpoint_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    monkeypatch.setenv("ENVIRONMENT", "development")
    from backend.app.config import get_settings

    get_settings.cache_clear()

    from backend.app.main import app
    from backend.app.schemas.pilot_auth import RegisterResponse

    aid = uuid4()

    def _fake_register(*, email: str, password: str) -> RegisterResponse:
        return RegisterResponse(
            advisor_id=aid,
            email=email.lower(),
            approval_status="PENDING_APPROVAL",
            message="ok",
        )

    monkeypatch.setattr(
        "backend.app.api.v1.auth_pilot.svc.register_pilot",
        _fake_register,
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/auth/register",
            json={"email": "Pilot@Example.COM", "password": "password123"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["approval_status"] == "PENDING_APPROVAL"
    assert body["email"] == "pilot@example.com"
    get_settings.cache_clear()


def test_request_access_page_served() -> None:
    from backend.app.main import app

    with TestClient(app) as client:
        page = client.get("/request-access")
        assert page.status_code == 200
        assert "text/html" in page.headers.get("content-type", "")
        assert b"Request pilot access" in page.content
        root = client.get("/", follow_redirects=False)
        assert root.status_code in (302, 307)
        assert root.headers.get("location") == "/request-access"
