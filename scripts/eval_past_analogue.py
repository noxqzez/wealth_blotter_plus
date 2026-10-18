#!/usr/bin/env python3
"""Score / compare Past Analogue golden-pack runs.

Baseline workflow
-----------------
1. Enable ``feature_flags.past_analogue`` (shadow). Leave TAVILY_API_KEY empty.
2. For each prompt in ``past_analogue_golden_scenarios.json``, run a scenario
   optimize and save the ``past_analogue`` artifact JSON::

       mkdir -p artifacts/past_analogue/baseline_ollama
       # copy each run's past_analogue blob to:
       # artifacts/past_analogue/baseline_ollama/<scenario_id>.json

3. Score::

       uv run python scripts/eval_past_analogue.py score \\
         --label baseline_ollama \\
         --run-dir artifacts/past_analogue/baseline_ollama

4. After adding Tavily (then Gemini), repeat into a new run-dir and::

       uv run python scripts/eval_past_analogue.py compare \\
         --run baseline_ollama=artifacts/past_analogue/baseline_ollama \\
         --run plus_tavily=artifacts/past_analogue/plus_tavily

Optional human scores JSON: ``{"rates_hike_50bp": 4, "negative_chitchat": 5}``
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.services.past_analogue_eval import (  # noqa: E402
    compare_runs,
    load_golden_scenarios,
    score_run_dir,
)


def _cmd_list(_: argparse.Namespace) -> int:
    scenarios, note = load_golden_scenarios()
    print(note)
    print()
    for s in scenarios:
        print(f"{s.id:24} [{s.bucket}] expect_admit={s.expect_admit}")
        print(f"  {s.prompt}")
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    human = None
    if args.human_scores:
        human = json.loads(Path(args.human_scores).read_text(encoding="utf-8"))
    summary = score_run_dir(
        args.label,
        Path(args.run_dir),
        human_scores=human,
    )
    out = Path(args.out) if args.out else None
    payload = summary.model_dump(mode="json")
    text = json.dumps(payload, indent=2)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {out}")
    else:
        print(text)
    print()
    print(
        f"Summary {summary.run_label}: expect_admit_acc="
        f"{summary.expect_admit_accuracy} cite={summary.mean_cite_rate:.2f} "
        f"ΔW_L1={summary.mean_delta_w_l1} (secondary)"
    )
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    summaries = []
    for item in args.run:
        if "=" not in item:
            raise SystemExit(f"--run must be label=dir, got {item!r}")
        label, path = item.split("=", 1)
        summaries.append(score_run_dir(label.strip(), Path(path.strip())))
    md = compare_runs(summaries)
    out = Path(args.out) if args.out else None
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md + "\n", encoding="utf-8")
        print(f"Wrote {out}")
    print(md)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List golden scenarios")
    p_list.set_defaults(func=_cmd_list)

    p_score = sub.add_parser("score", help="Score one run directory")
    p_score.add_argument("--label", required=True)
    p_score.add_argument("--run-dir", required=True)
    p_score.add_argument("--human-scores", default=None, help="JSON map scenario_id→1..5")
    p_score.add_argument("--out", default=None, help="Write summary JSON path")
    p_score.set_defaults(func=_cmd_score)

    p_cmp = sub.add_parser("compare", help="Compare multiple run directories")
    p_cmp.add_argument(
        "--run",
        action="append",
        required=True,
        help="label=path (repeatable)",
    )
    p_cmp.add_argument("--out", default=None, help="Write markdown path")
    p_cmp.set_defaults(func=_cmd_compare)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
