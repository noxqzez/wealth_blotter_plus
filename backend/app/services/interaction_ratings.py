"""Persist optional post-optimization star ratings (one per scenario_run)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from backend.app.core.supabase import get_service_supabase
from backend.app.services.usage_telemetry import record_usage_metric

logger = structlog.get_logger(__name__)


class InteractionRatingError(Exception):
    """Domain error for rating persistence."""

    def __init__(self, message: str, *, already_rated: bool = False) -> None:
        super().__init__(message)
        self.already_rated = already_rated


def _client():
    return get_service_supabase()


def has_rating_for_run(*, scenario_run_id: UUID) -> bool:
    """Return True if this simulation already has a stored rating."""
    try:
        result = (
            _client()
            .table("interaction_ratings")
            .select("id")
            .eq("scenario_run_id", str(scenario_run_id))
            .limit(1)
            .execute()
        )
        return bool(result.data)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "interaction_rating_lookup_failed",
            scenario_run_id=str(scenario_run_id),
            error=str(exc),
        )
        return False


def save_interaction_rating(
    *,
    advisor_id: UUID,
    scenario_run_id: UUID,
    rating: int,
    portfolio_id: UUID | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """
    Insert one rating for a simulation.

    Enforces rating ∈ [1, 5] and unique ``scenario_run_id`` (one per simulation).
    """
    if rating < 1 or rating > 5:
        raise InteractionRatingError("Rating must be an integer from 1 to 5.")

    if has_rating_for_run(scenario_run_id=scenario_run_id):
        raise InteractionRatingError(
            "This simulation was already rated.",
            already_rated=True,
        )

    payload: dict[str, Any] = {
        "advisor_id": str(advisor_id),
        "scenario_run_id": str(scenario_run_id),
        "rating": int(rating),
        "session_id": session_id,
    }
    if portfolio_id is not None:
        payload["portfolio_id"] = str(portfolio_id)

    try:
        result = _client().table("interaction_ratings").insert(payload).execute()
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "duplicate" in msg or "unique" in msg:
            raise InteractionRatingError(
                "This simulation was already rated.",
                already_rated=True,
            ) from exc
        logger.exception(
            "interaction_rating_insert_failed",
            scenario_run_id=str(scenario_run_id),
            error=str(exc),
        )
        raise InteractionRatingError(
            f"Failed to store rating: {exc}"
        ) from exc

    row = (result.data or [payload])[0]
    logger.info(
        "interaction_rating_saved",
        advisor_id=str(advisor_id),
        scenario_run_id=str(scenario_run_id),
        rating=rating,
    )
    # Lightweight product telemetry (best-effort; never blocks rating).
    record_usage_metric(
        event_action="interaction_rating",
        endpoint_or_component="copilot",
        advisor_id=advisor_id,
        metadata={
            "scenario_run_id": str(scenario_run_id),
            "rating": rating,
        },
    )

    return dict(row) if isinstance(row, dict) else payload


__all__ = [
    "InteractionRatingError",
    "has_rating_for_run",
    "save_interaction_rating",
]
