"""Supabase client helpers — user-scoped vs service-role."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from supabase import Client, create_client

from backend.app.config import Settings, get_settings


class SupabaseNotConfiguredError(RuntimeError):
    """Raised when required Supabase env vars are missing."""


def _require_url_and_key(url: str, key: str, label: str) -> None:
    if not url or not key:
        raise SupabaseNotConfiguredError(f"Supabase {label} client is not configured")


@lru_cache
def get_service_supabase() -> Client:
    """
    Service-role client for audit writes, config reads, and OMS queue updates.

    NEVER expose this key to frontends or API responses.
    """
    settings = get_settings()
    _require_url_and_key(settings.supabase_url, settings.supabase_service_role_key, "service")
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def get_user_supabase(access_token: str) -> Client:
    """
    User-scoped client so Postgres RLS (`auth.uid()`) applies.

    Pass the caller's Supabase Auth JWT (not the service role key).
    """
    settings = get_settings()
    _require_url_and_key(settings.supabase_url, settings.supabase_anon_key, "user")
    client = create_client(settings.supabase_url, settings.supabase_anon_key)
    client.postgrest.auth(access_token)
    return client


def service_table(table: str) -> Any:
    """Convenience accessor for service-role table operations."""
    return get_service_supabase().table(table)
