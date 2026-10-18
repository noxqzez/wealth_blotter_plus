"""Past Analogue unit tests (no network). Flag-off / admit / reweight / ΔW."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from backend.app.schemas.scenario import (
    PastAnalogueArtifact,
    PastAnaloguePeriodHit,
    WeightDecomposition,
    WeightDeltaComponent,
)
from backend.app.services.past_analogue import (
    admit_hits,
    blend_strategist_analogue_weights,
    build_weight_decomposition,
    format_chat_summary,
    past_analogue_apply_enabled,
    past_analogue_enabled,
    reweight_returns,
)


def _returns(n: int = 400) -> pd.DataFrame:
    idx = pd.bdate_range(end=date.today(), periods=n)
    rng = np.random.default_rng(0)
    data = rng.normal(0, 0.01, size=(n, 2))
    return pd.DataFrame(data, index=idx, columns=["AAA", "BBB"])


def test_past_analogue_disabled_by_default() -> None:
    assert past_analogue_enabled({}, {"phase": "phase1"}) is False
    assert past_analogue_enabled({"past_analogue": False}, {"phase": "phase1"}) is False


def test_past_analogue_enabled_when_flag_on() -> None:
    assert past_analogue_enabled(
        {"past_analogue": True}, {"phase": "phase1", "past_analogue_allowed": True}
    )


def test_past_analogue_locked_post_pilot() -> None:
    assert (
        past_analogue_enabled(
            {"past_analogue": True},
            {"phase": "post_pilot", "past_analogue_allowed": True},
        )
        is False
    )
    assert (
        past_analogue_enabled(
            {"past_analogue": True},
            {"phase": "phase1", "past_analogue_allowed": False},
        )
        is False
    )


def test_past_analogue_apply_follows_master_flag() -> None:
    """Option A: apply whenever past_analogue is on (phase1 included)."""
    cfg = {"phase": "phase1", "past_analogue_allowed": True}
    assert past_analogue_apply_enabled({"past_analogue": True}, cfg) is True
    assert past_analogue_apply_enabled({"past_analogue": False}, cfg) is False
    assert past_analogue_apply_enabled({}, cfg) is False


def test_admit_hits_threshold_and_yahoo_clip() -> None:
    rets = _returns(300)
    end = rets.index.max().date()
    start = end - timedelta(days=40)
    good = PastAnaloguePeriodHit(
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        label="ok",
        model_similarity=0.9,
    )
    bad_sim = PastAnaloguePeriodHit(
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        label="weak",
        model_similarity=0.05,
    )
    outside = PastAnaloguePeriodHit(
        start_date="1990-01-01",
        end_date="1990-06-01",
        label="old",
        model_similarity=0.99,
    )
    annotated, admitted = admit_hits(
        [good, bad_sim, outside], rets, threshold=0.35, top_k=5
    )
    assert len(annotated) == 3
    assert len(admitted) == 1
    assert admitted[0].label == "ok"
    assert admitted[0].admitted is True
    assert any(h.reject_reason and "below_threshold" in h.reject_reason for h in annotated)
    assert any(h.reject_reason == "outside_yahoo_panel" for h in annotated)


def test_reweight_keeps_rows() -> None:
    rets = _returns(200)
    end = rets.index.max().date()
    start = end - timedelta(days=30)
    hit = PastAnaloguePeriodHit(
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        label="w",
        model_similarity=0.8,
        validated_similarity=0.8,
        admitted=True,
        clipped_start=start.isoformat(),
        clipped_end=end.isoformat(),
    )
    out = reweight_returns(rets, [hit], trailing_blend=0.4)
    assert len(out) >= len(rets.iloc[-20:])
    assert list(out.columns) == ["AAA", "BBB"]


def test_weight_decomposition_shadow() -> None:
    base = {"AAA": 0.5, "BBB": 0.5}
    hist = {"AAA": 0.6, "BBB": 0.4}
    strat = {"AAA": 0.55, "BBB": 0.45}
    shadow = {"AAA": 0.7, "BBB": 0.3}
    decomp = build_weight_decomposition(
        w_base=base,
        w_hist=hist,
        w_strategist=strat,
        w_past_analogue=shadow,
        w_final=strat,
        past_analogue_applied=False,
    )
    names = {d.name: d.l1 for d in decomp.deltas}
    assert names["past_analogue"] > 0
    assert "shadow" in " ".join(decomp.notes).lower() or any(
        "shadow" in n for n in decomp.notes
    )


def test_blend_strategist_analogue_50_50() -> None:
    strat = {"AAA": 0.8, "BBB": 0.2}
    analogue = {"AAA": 0.2, "BBB": 0.8}
    blended = blend_strategist_analogue_weights(strat, analogue, analogue_mix=0.5)
    assert abs(blended["AAA"] - 0.5) < 1e-9
    assert abs(blended["BBB"] - 0.5) < 1e-9
    assert abs(sum(blended.values()) - 1.0) < 1e-9
    assert blend_strategist_analogue_weights(strat, analogue, analogue_mix=0.0) == strat
    full = blend_strategist_analogue_weights(strat, analogue, analogue_mix=1.0)
    assert abs(full["AAA"] - 0.2) < 1e-9


def test_format_chat_summary_one_line() -> None:
    art = PastAnalogueArtifact(
        enabled=True,
        weight_decomposition=WeightDecomposition(
            w_base={},
            w_final={},
            deltas=[
                WeightDeltaComponent(name="past_analogue", l1=0.125, per_ticker={})
            ],
        ),
    )
    assert format_chat_summary(art) == "weight input from historical events = 0.125"
