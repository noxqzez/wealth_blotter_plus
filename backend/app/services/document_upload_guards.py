"""Pilot RAG upload controls: kill switch, rate limit, daily page quota."""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog

from backend.app.config import Settings, get_settings
from backend.app.core.supabase import service_table
from backend.app.services.pdf_extract import count_pdf_pages

logger = structlog.get_logger(__name__)

_lock = threading.Lock()
# advisor_id -> recent upload timestamps (UTC)
_rate_windows: dict[str, deque[datetime]] = defaultdict(deque)


class DocumentUploadGuardError(RuntimeError):
    """Base for upload kill-switch / rate / quota failures."""


class DocumentUploadDisabledError(DocumentUploadGuardError):
    """RAG uploads turned off via RAG_UPLOAD_ENABLED."""


class DocumentUploadRateLimitError(DocumentUploadGuardError):
    """Too many uploads in the rolling minute window."""


class DocumentUploadQuotaError(DocumentUploadGuardError):
    """Daily page quota exceeded (or single file too large)."""


def _utc_day_start(now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    return current.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _check_rate_limit(advisor_id: UUID, *, settings: Settings) -> None:
    limit = int(settings.rag_upload_rate_limit_per_minute)
    key = str(advisor_id)
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=1)
    with _lock:
        window = _rate_windows[key]
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= limit:
            raise DocumentUploadRateLimitError(
                f"Upload rate limit exceeded ({limit}/minute). Try again shortly."
            )
        window.append(now)


def _pages_used_today(advisor_id: UUID, *, day_start: datetime) -> int:
    """Sum page_count for this advisor's uploads since UTC midnight."""
    try:
        res = (
            service_table("document_ingest_jobs")
            .select("page_count")
            .eq("advisor_id", str(advisor_id))
            .gte("created_at", day_start.isoformat())
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 — column may be missing pre-migration
        logger.warning(
            "rag_upload_quota_query_failed",
            error=str(exc),
            hint="Apply migration_rag_upload_guards.sql for durable page quotas",
        )
        return 0

    total = 0
    for row in res.data or []:
        raw = row.get("page_count") if isinstance(row, dict) else None
        if raw is None:
            continue
        try:
            total += max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return total


def enforce_rag_upload_guards(
    *,
    advisor_id: UUID,
    content: bytes,
    settings: Settings | None = None,
) -> int:
    """
    Validate kill switch / rate / page quotas before Storage write.

    Returns page count of this PDF.
    """
    cfg = settings or get_settings()
    if not cfg.rag_upload_enabled:
        raise DocumentUploadDisabledError(
            "Research PDF uploads are disabled (RAG_UPLOAD_ENABLED=false)."
        )

    _check_rate_limit(advisor_id, settings=cfg)

    try:
        page_count = count_pdf_pages(content)
    except ImportError as exc:
        raise DocumentUploadGuardError(
            "PDF tooling unavailable. Run: uv sync --extra rag"
        ) from exc
    except DocumentUploadGuardError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DocumentUploadGuardError(
            f"Could not read PDF page count: {exc}"
        ) from exc

    if page_count <= 0:
        raise DocumentUploadGuardError("PDF has no pages")

    max_file = int(cfg.rag_upload_max_pages_per_file)
    if page_count > max_file:
        raise DocumentUploadQuotaError(
            f"PDF has {page_count} pages; max per file is {max_file}."
        )

    day_start = _utc_day_start()
    used = _pages_used_today(advisor_id, day_start=day_start)
    daily_cap = int(cfg.rag_upload_max_pages_per_day)
    if used + page_count > daily_cap:
        raise DocumentUploadQuotaError(
            f"Daily page quota exceeded: used {used}/{daily_cap} pages today "
            f"(UTC); this file adds {page_count}."
        )

    logger.info(
        "rag_upload_guard_ok",
        advisor_id=str(advisor_id),
        page_count=page_count,
        pages_used_today=used,
        daily_cap=daily_cap,
    )
    return page_count
