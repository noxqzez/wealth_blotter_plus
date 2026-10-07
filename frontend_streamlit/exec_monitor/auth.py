"""Manager-only gate for the Usage & Control Monitor."""

from __future__ import annotations

import os
from typing import Any

import streamlit as st


def _secret(name: str, default: str = "") -> str:
    env = os.environ.get(name, "")
    if env:
        return env
    try:
        return str(st.secrets.get(name, default))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return default


def manager_bypass_enabled() -> bool:
    raw = _secret("DEV_MANAGER_BYPASS", os.environ.get("DEV_MANAGER_BYPASS", "")).lower()
    return raw in {"1", "true", "yes", "on"}


def allowed_manager_ids() -> set[str]:
    raw = _secret("MANAGER_USER_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def allowed_manager_emails() -> set[str]:
    raw = _secret("MANAGER_EMAILS", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def require_manager() -> dict[str, Any] | None:
    """
    Return a small manager identity dict, or None if access denied.

    Local demos: set ``DEV_MANAGER_BYPASS=true``.
    Production: set ``MANAGER_USER_IDS`` and/or ``MANAGER_EMAILS`` (comma-separated)
    matching the authenticated Supabase user. JWT may be provided via
    ``WBP_API_JWT`` / secrets for identity display; service-role reads are separate.
    """
    if manager_bypass_enabled():
        return {
            "id": "dev-manager",
            "email": "manager@local.dev",
            "role": "manager",
            "bypass": True,
        }

    ids = allowed_manager_ids()
    emails = allowed_manager_emails()
    if not ids and not emails:
        st.error(
            "Manager access is not configured. Set `MANAGER_USER_IDS` / "
            "`MANAGER_EMAILS` in secrets, or `DEV_MANAGER_BYPASS=true` for local demos."
        )
        return None

    # Optional: resolve identity from JWT via Supabase Auth if configured.
    user_id = _secret("WBP_MANAGER_USER_ID", "")
    user_email = _secret("WBP_MANAGER_EMAIL", "").lower()
    if user_id and user_id in ids:
        return {"id": user_id, "email": user_email or None, "role": "manager", "bypass": False}
    if user_email and user_email in emails:
        return {"id": user_id or user_email, "email": user_email, "role": "manager", "bypass": False}

    st.error("Signed-in user is not on the manager allowlist.")
    st.caption("Configure MANAGER_USER_IDS / MANAGER_EMAILS or enable DEV_MANAGER_BYPASS.")
    return None
