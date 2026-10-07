"""Unit tests for interaction rating helpers (no DB)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend.app.services.interaction_ratings import (
    InteractionRatingError,
    save_interaction_rating,
)


def test_save_interaction_rating_rejects_out_of_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "backend.app.services.interaction_ratings.has_rating_for_run",
        lambda **_kwargs: False,
    )
    with pytest.raises(InteractionRatingError, match="1 to 5"):
        save_interaction_rating(
            advisor_id=uuid4(),
            scenario_run_id=uuid4(),
            rating=0,
        )
    with pytest.raises(InteractionRatingError, match="1 to 5"):
        save_interaction_rating(
            advisor_id=uuid4(),
            scenario_run_id=uuid4(),
            rating=6,
        )


def test_save_interaction_rating_rejects_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "backend.app.services.interaction_ratings.has_rating_for_run",
        lambda **_kwargs: True,
    )
    with pytest.raises(InteractionRatingError, match="already rated") as exc:
        save_interaction_rating(
            advisor_id=uuid4(),
            scenario_run_id=uuid4(),
            rating=5,
        )
    assert exc.value.already_rated is True
