"""SSO extension tests — default-off, authorize/complete gated."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DEV_AUTH_BYPASS", "false")
    monkeypatch.setenv("SSO_ENABLED", "false")
    monkeypatch.setenv("SSO_PROVIDERS", "")
    from backend.app.config import get_settings

    get_settings.cache_clear()
    from backend.app.main import app

    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def test_sso_status_disabled_by_default(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/sso/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["providers"] == []
    assert body["require_approval"] is True


def test_sso_authorize_forbidden_when_disabled(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/sso/authorize?provider=google")
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error_code"] == "WBP-AUTH-SSO-DISABLED"


def test_sso_complete_forbidden_when_disabled(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/sso/complete",
        json={"access_token": "x" * 40},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "WBP-AUTH-SSO-DISABLED"


def test_sso_start_forbidden_when_disabled(client: TestClient) -> None:
    resp = client.get("/auth/sso/start?provider=google", follow_redirects=False)
    assert resp.status_code == 403


def test_sso_callback_page_served(client: TestClient) -> None:
    resp = client.get("/auth/sso/callback")
    assert resp.status_code == 200
    assert b"Signing you in" in resp.content


def test_sso_authorize_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("SSO_ENABLED", "true")
    monkeypatch.setenv("SSO_PROVIDERS", "google,azure")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-test-key")
    from backend.app.config import get_settings

    get_settings.cache_clear()
    from backend.app.main import app

    with TestClient(app) as client:
        status = client.get("/api/v1/auth/sso/status")
        assert status.json()["enabled"] is True
        assert status.json()["providers"] == ["google", "azure"]

        authz = client.get("/api/v1/auth/sso/authorize?provider=google")
        assert authz.status_code == 200
        body = authz.json()
        assert "authorize" in body["authorize_url"]
        assert "provider=google" in body["authorize_url"]
        assert body["redirect_to"].endswith("/auth/sso/callback")

        bad = client.get("/api/v1/auth/sso/authorize?provider=facebook")
        assert bad.status_code == 400
    get_settings.cache_clear()


def test_sso_complete_pending_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("SSO_ENABLED", "true")
    monkeypatch.setenv("SSO_PROVIDERS", "google")
    monkeypatch.setenv("SSO_REQUIRE_APPROVAL", "true")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret-for-jwt-hs256-tests")
    from backend.app.config import get_settings

    get_settings.cache_clear()
    from backend.app.main import app

    aid = uuid4()

    def _pending(_advisor_id):  # noqa: ANN001
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "WBP-AUTH-PENDING",
                "message": "pending",
            },
        )

    with (
        patch(
            "backend.app.services.pilot_sso.decode_supabase_jwt",
            return_value={"sub": str(aid), "email": "sso@firm.com"},
        ),
        patch(
            "backend.app.services.pilot_sso.ensure_sso_profile",
            return_value={
                "id": str(aid),
                "email": "sso@firm.com",
                "role": "advisor",
                "approval_status": "PENDING_APPROVAL",
            },
        ),
        patch(
            "backend.app.services.pilot_sso.require_approved_profile",
            side_effect=_pending,
        ),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/api/v1/auth/sso/complete",
            json={"access_token": "eyJhbGciOiJIUzI1NiJ9." + "x" * 20},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error_code"] == "WBP-AUTH-PENDING"
    get_settings.cache_clear()
