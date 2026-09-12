"""Fetch and cache Rules-as-Code configs from Supabase `configs` table."""

from __future__ import annotations

import time
from typing import Any

import structlog

from backend.app.config import Settings, get_settings
from backend.app.core.supabase import SupabaseNotConfiguredError, get_service_supabase
from backend.app.schemas.rules import BusinessGuardrails, TechnicalLimits

logger = structlog.get_logger(__name__)


class RulesEngineError(Exception):
    """Raised when a technical limit or config load fails at the API boundary."""


class RulesEngine:
    """TTL-cached reader for `technical_limits` and `business_guardrails`."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._cache: dict[str, Any] = {}
        self._loaded_at: float = 0.0

    @property
    def _ttl(self) -> int:
        return self._settings.rules_cache_ttl_seconds

    def _is_stale(self) -> bool:
        if not self._cache:
            return True
        return (time.monotonic() - self._loaded_at) > self._ttl

    async def refresh(self) -> None:
        """Load configs via service role. Falls back to in-repo seed defaults."""
        try:
            client = get_service_supabase()
            result = client.table("configs").select("key,config").execute()
            rows = {row["key"]: row["config"] for row in (result.data or [])}
            if "technical_limits" in rows:
                self._cache["technical_limits"] = TechnicalLimits.model_validate(
                    rows["technical_limits"]
                )
            if "business_guardrails" in rows:
                self._cache["business_guardrails"] = BusinessGuardrails.model_validate(
                    rows["business_guardrails"]
                )
            self._loaded_at = time.monotonic()
            logger.info("rules_refreshed_from_db", keys=list(self._cache.keys()))
        except (SupabaseNotConfiguredError, Exception) as exc:  # noqa: BLE001
            logger.warning("rules_db_unavailable_using_defaults", error=str(exc))
            self._cache["technical_limits"] = TechnicalLimits()
            self._cache["business_guardrails"] = BusinessGuardrails()
            self._loaded_at = time.monotonic()

    async def get_technical_limits(self) -> TechnicalLimits:
        if self._is_stale():
            await self.refresh()
        limits = self._cache.get("technical_limits")
        if not isinstance(limits, TechnicalLimits):
            raise RulesEngineError("technical_limits config missing")
        return limits

    async def get_business_guardrails(self) -> BusinessGuardrails:
        if self._is_stale():
            await self.refresh()
        guards = self._cache.get("business_guardrails")
        if not isinstance(guards, BusinessGuardrails):
            raise RulesEngineError("business_guardrails config missing")
        return guards


_engine: RulesEngine | None = None


def get_rules_engine() -> RulesEngine:
    global _engine
    if _engine is None:
        _engine = RulesEngine()
    return _engine
