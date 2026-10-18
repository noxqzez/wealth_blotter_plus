"""Pilot welcome email + admin approve flow (mocked Resend / Supabase)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.schemas.pilot_admin import ApproveAndWelcomeRequest
from backend.app.services.pilot_welcome_email import (
    WelcomeEmailError,
    build_welcome_text,
    default_manifest_url,
    send_pilot_welcome_email,
)


def test_welcome_text_includes_manifest_and_no_password() -> None:
    text = build_welcome_text(
        manifest_url="https://example.com/manifest.pilot.xml"
    )
    assert "https://example.com/manifest.pilot.xml" in text
    assert "password you registered" in text.lower() or "same email" in text.lower()
    assert "Primary LLM" in text
    assert "Secondary LLM" in text
    assert "Ollama" in text
    assert "sk-" not in text


def test_default_manifest_url_prefers_explicit() -> None:
    settings = MagicMock()
    settings.pilot_manifest_url = "https://cdn.example/manifest.pilot.xml"
    settings.pilot_excel_base_url = "https://ignored.example"
    assert default_manifest_url(settings).endswith("manifest.pilot.xml")


def test_send_welcome_requires_resend_config() -> None:
    settings = MagicMock()
    settings.resend_api_key = ""
    settings.pilot_welcome_from_email = "pilots@example.com"
    settings.pilot_manifest_url = "https://example.com/manifest.pilot.xml"
    with pytest.raises(WelcomeEmailError, match="RESEND_API_KEY"):
        send_pilot_welcome_email(to_email="a@b.com", settings=settings)


@patch("backend.app.services.pilot_welcome_email.httpx.Client")
def test_send_welcome_posts_to_resend(mock_client_cls: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "re_123"}
    mock_client.post.return_value = mock_resp

    settings = MagicMock()
    settings.resend_api_key = "re_test"
    settings.pilot_welcome_from_email = "Wealth <pilots@example.com>"
    settings.pilot_manifest_url = "https://example.com/manifest.pilot.xml"
    settings.pilot_excel_base_url = ""

    out = send_pilot_welcome_email(to_email="Pilot@Example.COM", settings=settings)
    assert out.to_email == "pilot@example.com"
    assert out.provider_id == "re_123"
    kwargs = mock_client.post.call_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer re_test"
    payload = kwargs["json"]
    assert payload["to"] == ["pilot@example.com"]
    assert "attachments" in payload
    assert payload["attachments"][0]["filename"].endswith(".csv")


@patch("backend.app.services.pilot_admin.send_pilot_welcome_email")
@patch("backend.app.services.pilot_admin.get_service_supabase")
def test_approve_and_welcome_sends_once(
    mock_sb: MagicMock,
    mock_send: MagicMock,
) -> None:
    from backend.app.services.pilot_admin import approve_and_welcome

    aid = uuid4()
    table = MagicMock()
    client = MagicMock()
    client.table.return_value = table
    mock_sb.return_value = client

    select_chain = MagicMock()
    table.select.return_value = select_chain
    select_chain.ilike.return_value = select_chain
    select_chain.limit.return_value = select_chain
    select_chain.execute.return_value = MagicMock(
        data=[
            {
                "id": str(aid),
                "email": "pilot@example.com",
                "approval_status": "PENDING_APPROVAL",
                "welcome_email_sent_at": None,
                "role": "advisor",
            }
        ]
    )
    table.update.return_value = table
    table.eq.return_value = table
    table.execute.return_value = MagicMock(data=[])

    settings = MagicMock()
    result = approve_and_welcome(
        ApproveAndWelcomeRequest(email="pilot@example.com"),
        settings=settings,
    )
    assert result.email_sent is True
    assert result.approval_status == "APPROVED"
    mock_send.assert_called_once()


@patch("backend.app.services.pilot_admin.send_pilot_welcome_email")
@patch("backend.app.services.pilot_admin.get_service_supabase")
def test_approve_and_welcome_skips_when_already_sent(
    mock_sb: MagicMock,
    mock_send: MagicMock,
) -> None:
    from backend.app.services.pilot_admin import approve_and_welcome

    aid = uuid4()
    table = MagicMock()
    client = MagicMock()
    client.table.return_value = table
    mock_sb.return_value = client

    select_chain = MagicMock()
    table.select.return_value = select_chain
    select_chain.ilike.return_value = select_chain
    select_chain.limit.return_value = select_chain
    select_chain.execute.return_value = MagicMock(
        data=[
            {
                "id": str(aid),
                "email": "pilot@example.com",
                "approval_status": "APPROVED",
                "welcome_email_sent_at": "2026-01-01T00:00:00Z",
                "role": "advisor",
            }
        ]
    )
    table.update.return_value = table
    table.eq.return_value = table
    table.execute.return_value = MagicMock(data=[])

    result = approve_and_welcome(
        ApproveAndWelcomeRequest(email="pilot@example.com", force_resend=False),
        settings=MagicMock(),
    )
    assert result.email_sent is False
    assert result.email_skipped is True
    assert result.skip_reason == "already_sent"
    mock_send.assert_not_called()


def test_admin_endpoint_requires_key() -> None:
    from backend.app.main import app

    client = TestClient(app)
    with patch("backend.app.api.v1.admin_pilots.get_settings") as gs:
        settings = MagicMock()
        settings.admin_api_key = "secret-admin"
        settings.environment = "development"
        gs.return_value = settings
        res = client.post(
            "/api/v1/admin/pilots/approve-and-welcome",
            json={"email": "pilot@example.com"},
        )
    assert res.status_code == 401


def test_admin_endpoint_rejects_wrong_key() -> None:
    from backend.app.main import app

    client = TestClient(app)
    with patch("backend.app.api.v1.admin_pilots.get_settings") as gs:
        settings = MagicMock()
        settings.admin_api_key = "secret-admin"
        settings.environment = "development"
        gs.return_value = settings
        res = client.post(
            "/api/v1/admin/pilots/approve-and-welcome",
            json={"email": "pilot@example.com"},
            headers={"X-Admin-Key": "wrong"},
        )
    assert res.status_code == 401


def test_admin_endpoint_ok() -> None:
    from backend.app.main import app
    from backend.app.schemas.pilot_admin import ApproveAndWelcomeResponse

    aid = uuid4()
    client = TestClient(app)
    fake = ApproveAndWelcomeResponse(
        advisor_id=aid,
        email="pilot@example.com",
        approval_status="APPROVED",
        email_sent=True,
        email_skipped=False,
        skip_reason=None,
        message="Pilot approved and welcome email sent.",
    )
    with (
        patch("backend.app.api.v1.admin_pilots.get_settings") as gs,
        patch(
            "backend.app.api.v1.admin_pilots.admin_svc.approve_and_welcome",
            return_value=fake,
        ),
    ):
        settings = MagicMock()
        settings.admin_api_key = "secret-admin"
        settings.environment = "development"
        gs.return_value = settings
        res = client.post(
            "/api/v1/admin/pilots/approve-and-welcome",
            json={"email": "pilot@example.com"},
            headers={"X-Admin-Key": "secret-admin"},
        )
    assert res.status_code == 200
    assert res.json()["email_sent"] is True
