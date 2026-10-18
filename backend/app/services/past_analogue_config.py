"""Backward-compatible re-exports — prefer ``backend.app.services.past_analogue``."""

from __future__ import annotations

from backend.app.services.past_analogue import (
    DEFAULT_ANALOGUE_MIX,
    DEFAULT_ARCHIVE_DAYS,
    DEFAULT_ARCHIVE_YEARS,
    DEFAULT_MAX_WINDOW_DAYS,
    DEFAULT_SEARCH_TIMEOUT_MS,
    DEFAULT_SIMILARITY_THRESHOLD,
    DEFAULT_TOP_K,
    DEFAULT_TRAILING_BLEND,
    past_analogue_allowed,
    past_analogue_apply_enabled,
    past_analogue_enabled,
    past_analogue_knobs,
)

__all__ = [
    "DEFAULT_ANALOGUE_MIX",
    "DEFAULT_ARCHIVE_DAYS",
    "DEFAULT_ARCHIVE_YEARS",
    "DEFAULT_MAX_WINDOW_DAYS",
    "DEFAULT_SEARCH_TIMEOUT_MS",
    "DEFAULT_SIMILARITY_THRESHOLD",
    "DEFAULT_TOP_K",
    "DEFAULT_TRAILING_BLEND",
    "past_analogue_allowed",
    "past_analogue_apply_enabled",
    "past_analogue_enabled",
    "past_analogue_knobs",
]
