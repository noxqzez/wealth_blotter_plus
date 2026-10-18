"""Past Analogue A/B scoring — quality first, ΔW L1 secondary.

Larger ``DeltaW_past_analogue`` is NOT an improvement signal by itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, Field

_GOLDEN_PATH = (
    Path(__file__).resolve().parents[1]
    / "db"
    / "seed_configs"
    / "past_analogue_golden_scenarios.json"
)


class GoldenScenario(BaseModel):
    id: str
    bucket: str
    prompt: str
    expect_admit: bool = True
    human_rubric: str = ""


class ScenarioAutoScore(BaseModel):
    scenario_id: str
    bucket: str = ""
    expect_admit: bool = True
    parse_ok: bool = False
    n_raw_hits: int = 0
    n_admitted: int = 0
    admit_rate: float = 0.0
    mean_validated_similarity: float | None = None
    n_with_source_url: int = 0
    cite_rate: float = 0.0
    delta_w_past_analogue_l1: float | None = None
    expect_admit_match: bool | None = None
    human_relevance_1_to_5: int | None = Field(
        default=None,
        ge=1,
        le=5,
        description="Filled manually after review; not auto-computed.",
    )
    notes: list[str] = Field(default_factory=list)


class EvalRunSummary(BaseModel):
    run_label: str
    n_scenarios: int = 0
    mean_admit_rate: float = 0.0
    mean_cite_rate: float = 0.0
    expect_admit_accuracy: float | None = None
    mean_delta_w_l1: float | None = None
    mean_human_relevance: float | None = None
    scoring_note: str = (
        "Do not rank providers by mean_delta_w_l1 alone. "
        "Prefer expect_admit_accuracy, cite_rate, and mean_human_relevance."
    )
    scenarios: list[ScenarioAutoScore] = Field(default_factory=list)


def load_golden_scenarios(
    path: Path | None = None,
) -> tuple[list[GoldenScenario], str]:
    p = path or _GOLDEN_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    note = str(data.get("scoring_note") or "")
    scenarios = [GoldenScenario.model_validate(s) for s in data.get("scenarios") or []]
    return scenarios, note


def _l1_from_artifact(artifact: Mapping[str, Any]) -> float | None:
    decomp = artifact.get("weight_decomposition") or {}
    deltas = decomp.get("deltas") if isinstance(decomp, dict) else None
    if not isinstance(deltas, list):
        # Fallback: ||shadow - empty|| if only weights_shadow present
        shadow = artifact.get("weights_shadow") or {}
        if isinstance(shadow, dict) and shadow:
            return float(sum(abs(float(v)) for v in shadow.values()))
        return None
    for d in deltas:
        if isinstance(d, dict) and d.get("name") == "past_analogue":
            return float(d.get("l1") or 0.0)
    return None


def score_artifact(
    scenario: GoldenScenario,
    artifact: Mapping[str, Any] | None,
    *,
    human_relevance_1_to_5: int | None = None,
) -> ScenarioAutoScore:
    """Score one frozen Past Analogue artifact for a golden scenario."""
    notes: list[str] = []
    if not artifact:
        return ScenarioAutoScore(
            scenario_id=scenario.id,
            bucket=scenario.bucket,
            expect_admit=scenario.expect_admit,
            parse_ok=False,
            expect_admit_match=False if scenario.expect_admit else True,
            notes=["missing_artifact"],
            human_relevance_1_to_5=human_relevance_1_to_5,
        )

    raw = artifact.get("raw_hits") or []
    admitted = artifact.get("admitted_hits") or []
    if not isinstance(raw, list):
        raw = []
    if not isinstance(admitted, list):
        admitted = []

    llm_raw = artifact.get("llm_raw_text")
    parse_ok = bool(llm_raw) or bool(raw) or int(artifact.get("llm_parse_attempts") or 0) > 0
    if artifact.get("messages"):
        for m in artifact["messages"]:
            if "fail" in str(m).lower() and "fail-open" in str(m).lower():
                notes.append(str(m)[:200])

    n_raw = len(raw)
    n_adm = len(admitted)
    admit_rate = float(n_adm) / float(n_raw) if n_raw else (1.0 if n_adm else 0.0)

    sims: list[float] = []
    n_cite = 0
    for h in admitted:
        if not isinstance(h, dict):
            continue
        vs = h.get("validated_similarity")
        if vs is None:
            vs = h.get("model_similarity")
        try:
            sims.append(float(vs))
        except (TypeError, ValueError):
            pass
        urls = h.get("source_urls") or []
        if isinstance(urls, list) and any(str(u).startswith("http") for u in urls):
            n_cite += 1

    cite_rate = float(n_cite) / float(n_adm) if n_adm else 0.0
    mean_sim = sum(sims) / len(sims) if sims else None
    delta_l1 = _l1_from_artifact(artifact)

    got_admit = n_adm > 0
    expect_match = got_admit == bool(scenario.expect_admit)

    return ScenarioAutoScore(
        scenario_id=scenario.id,
        bucket=scenario.bucket,
        expect_admit=scenario.expect_admit,
        parse_ok=parse_ok,
        n_raw_hits=n_raw,
        n_admitted=n_adm,
        admit_rate=admit_rate,
        mean_validated_similarity=mean_sim,
        n_with_source_url=n_cite,
        cite_rate=cite_rate,
        delta_w_past_analogue_l1=delta_l1,
        expect_admit_match=expect_match,
        human_relevance_1_to_5=human_relevance_1_to_5,
        notes=notes,
    )


def summarize_run(
    run_label: str,
    scores: Sequence[ScenarioAutoScore],
    *,
    scoring_note: str = "",
) -> EvalRunSummary:
    rows = list(scores)
    n = len(rows)
    if n == 0:
        return EvalRunSummary(run_label=run_label, scoring_note=scoring_note)

    mean_admit = sum(s.admit_rate for s in rows) / n
    mean_cite = sum(s.cite_rate for s in rows) / n
    matched = [s.expect_admit_match for s in rows if s.expect_admit_match is not None]
    acc = sum(1 for m in matched if m) / len(matched) if matched else None
    l1s = [s.delta_w_past_analogue_l1 for s in rows if s.delta_w_past_analogue_l1 is not None]
    mean_l1 = sum(l1s) / len(l1s) if l1s else None
    hum = [s.human_relevance_1_to_5 for s in rows if s.human_relevance_1_to_5 is not None]
    mean_hum = sum(hum) / len(hum) if hum else None

    return EvalRunSummary(
        run_label=run_label,
        n_scenarios=n,
        mean_admit_rate=mean_admit,
        mean_cite_rate=mean_cite,
        expect_admit_accuracy=acc,
        mean_delta_w_l1=mean_l1,
        mean_human_relevance=mean_hum,
        scoring_note=scoring_note
        or (
            "Do not rank providers by mean_delta_w_l1 alone. "
            "Prefer expect_admit_accuracy, cite_rate, and mean_human_relevance."
        ),
        scenarios=list(rows),
    )


def compare_runs(summaries: Sequence[EvalRunSummary]) -> str:
    """Markdown table comparing labeled runs."""
    lines = [
        "# Past Analogue eval comparison",
        "",
        summaries[0].scoring_note if summaries else "",
        "",
        "| run | n | expect_admit_acc | mean_cite_rate | mean_admit_rate | mean_ΔW_L1 (secondary) | mean_human_rel |",
        "|-----|---|------------------|----------------|-----------------|-------------------------|----------------|",
    ]
    for s in summaries:
        acc = f"{s.expect_admit_accuracy:.2f}" if s.expect_admit_accuracy is not None else "—"
        l1 = f"{s.mean_delta_w_l1:.4f}" if s.mean_delta_w_l1 is not None else "—"
        hum = f"{s.mean_human_relevance:.2f}" if s.mean_human_relevance is not None else "—"
        lines.append(
            f"| {s.run_label} | {s.n_scenarios} | {acc} | {s.mean_cite_rate:.2f} | "
            f"{s.mean_admit_rate:.2f} | {l1} | {hum} |"
        )
    lines.append("")
    lines.append(
        "Primary decision metrics: **expect_admit_accuracy**, **mean_cite_rate**, "
        "**mean_human_relevance**. Use mean_ΔW_L1 only to show measurable impact, not quality."
    )
    return "\n".join(lines)


def load_run_dir(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Load ``{scenario_id}.json`` artifacts from a directory."""
    out: dict[str, dict[str, Any]] = {}
    if not run_dir.is_dir():
        return out
    for path in sorted(run_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # Allow either bare artifact or {"past_analogue": {...}}
        art = data.get("past_analogue") if isinstance(data, dict) else None
        if isinstance(art, dict):
            out[path.stem] = art
        elif isinstance(data, dict):
            out[path.stem] = data
    return out


def score_run_dir(
    run_label: str,
    run_dir: Path,
    *,
    golden_path: Path | None = None,
    human_scores: Mapping[str, int] | None = None,
) -> EvalRunSummary:
    scenarios, note = load_golden_scenarios(golden_path)
    artifacts = load_run_dir(run_dir)
    human_scores = dict(human_scores or {})
    scores = [
        score_artifact(
            sc,
            artifacts.get(sc.id),
            human_relevance_1_to_5=human_scores.get(sc.id),
        )
        for sc in scenarios
    ]
    return summarize_run(run_label, scores, scoring_note=note)
