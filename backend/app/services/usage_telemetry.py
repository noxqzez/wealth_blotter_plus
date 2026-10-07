"""Best-effort product telemetry inserts (no secrets / prompts)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from backend.app.core.supabase import get_service_supabase

logger = structlog.get_logger(__name__)


def record_usage_metric(
    *,
    event_action: str,
    endpoint_or_component: str,
    advisor_id: UUID | None = None,
    execution_time_ms: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Insert usage_metrics row; never raises to the caller."""
    try:
        row: dict[str, Any] = {
            "event_action": event_action,
            "endpoint_or_component": endpoint_or_component[:256],
            "metadata": metadata or {},
        }
        if advisor_id is not None:
            row["advisor_id"] = str(advisor_id)
        if execution_time_ms is not None:
            row["execution_time_ms"] = int(execution_time_ms)
        get_service_supabase().table("usage_metrics").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("usage_metrics_insert_failed", error=str(exc))
