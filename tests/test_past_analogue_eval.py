"""Past Analogue eval scorer tests (no network)."""

from __future__ import annotations

import json
from pathlib import Path

from backend.app.services.past_analogue_eval import (
    GoldenScenario,
    compare_runs,
    load_golden_scenarios,
    score_artifact,
    score_run_dir,
    summarize_run,
)


def test_load_golden_pack() -> None:
    scenarios, note = load_golden_scenarios()
    assert len(scenarios) >= 8
    assert "DeltaW" in note or "L1" in note or "not" in note.lower()
    ids = {s.id for s in scenarios}
    assert "rates_hike_50bp" in ids
    assert "negative_chitchat" in ids
    neg = next(s for s in scenarios if s.id == "negative_chitchat")
    assert neg.expect_admit is False


def test_score_artifact_expect_admit() -> None:
    sc = GoldenScenario(
        id="rates_hike_50bp",
        bucket="quantified_macro",
        prompt="rates up",
        expect_admit=True,
    )
    art = {
        "llm_raw_text": '{"hits":[]}',
        "llm_parse_attempts": 1,
        "raw_hits": [
            {
                "start_date": "2022-06-01",
                "end_date": "2022-07-15",
                "model_similarity": 0.8,
                "source_urls": [],
            }
        ],
        "admitted_hits": [
            {
                "start_date": "2022-06-01",
                "end_date": "2022-07-15",
                "validated_similarity": 0.7,
                "source_urls": ["https://example.com/fed"],
            }
        ],
        "weight_decomposition": {
            "deltas": [{"name": "past_analogue", "l1": 0.12, "per_ticker": {}}]
        },
    }
    s = score_artifact(sc, art, human_relevance_1_to_5=4)
    assert s.parse_ok
    assert s.n_admitted == 1
    assert s.cite_rate == 1.0
    assert s.expect_admit_match is True
    assert s.delta_w_past_analogue_l1 == 0.12
    assert s.human_relevance_1_to_5 == 4


def test_score_negative_control() -> None:
    sc = GoldenScenario(
        id="negative_chitchat",
        bucket="negative_control",
        prompt="weather",
        expect_admit=False,
    )
    empty = {"raw_hits": [], "admitted_hits": [], "llm_parse_attempts": 1, "llm_raw_text": "{}"}
    s = score_artifact(sc, empty)
    assert s.expect_admit_match is True
    assert s.n_admitted == 0


def test_compare_runs_markdown(tmp_path: Path) -> None:
    art_good = {
        "llm_raw_text": "{}",
        "raw_hits": [{}],
        "admitted_hits": [
            {"validated_similarity": 0.5, "source_urls": ["https://x.test"]}
        ],
        "weight_decomposition": {"deltas": [{"name": "past_analogue", "l1": 0.01}]},
    }
    d1 = tmp_path / "run_a"
    d1.mkdir()
    # Only write one scenario file — scorer still iterates full golden pack
    (d1 / "rates_hike_50bp.json").write_text(json.dumps(art_good), encoding="utf-8")

    summary = score_run_dir("run_a", d1)
    assert summary.n_scenarios >= 8
    md = compare_runs([summary])
    assert "expect_admit_acc" in md
    assert "secondary" in md.lower() or "ΔW" in md


def test_summarize_prefers_accuracy_fields() -> None:
    sc = GoldenScenario(id="x", bucket="b", prompt="p", expect_admit=True)
    s1 = score_artifact(sc, None)
    summary = summarize_run("t", [s1])
    assert "mean_delta_w_l1" in summary.scoring_note.lower() or "alone" in summary.scoring_note
