# Past Analogue evaluation (Ollama → Tavily → Gemini)

## Goal

Compare provider stacks on a **frozen golden pack** without treating larger
weight moves as “better.”

**Primary metrics:** expect-admit accuracy, citation rate, human relevance (1–5).  
**Secondary:** L1 \(\Delta W_{\mathrm{past\_analogue}}\) (shows impact, not quality).

## Phases

| Phase | Search | LLM extract | Env |
|-------|--------|-------------|-----|
| A – baseline | Public fallback (no Tavily) | Ollama (default `build_llm`) | `TAVILY_API_KEY=` `GEMINI_API_KEY=` |
| B – +Tavily | Tavily | Ollama | set `TAVILY_API_KEY` |
| C – +Gemini | Tavily (or fallback) | Gemini when wired | set `GEMINI_API_KEY` + `PAST_ANALOGUE_LLM_PROVIDER=gemini` |
| D – both | Tavily | Gemini | both keys |

## Pilot credit budget (Tavily)

Keep usage for **golden-pack A/B only** — not everyday advisor runs.

- **Phase A first** with `TAVILY_API_KEY` unset (or comment it out) so baseline costs **0** Tavily credits.
- Phase B: ~**1 live call per golden scenario** (`PAST_ANALOGUE_MAX_SEARCH_QUERIES=1`). Full pack ≈ **10 calls**. Reruns of the same prompt hit disk cache under `TAVILY_CACHE_DIR` → **0** extra credits.
- Hard stop: `TAVILY_MAX_CALLS_PER_PROCESS=40` (default) blocks further live HTTP after the cap.
- Turn Past Analogue **off** in Streamlit when not evaluating so normal pilot traffic does not search.
- Do not point Excel advisors at a process with Tavily enabled until you finish the A/B pack.

`GEMINI_API_KEY` / `PAST_ANALOGUE_LLM_PROVIDER=gemini` are **placeholders** today —
extract still uses Ollama/BYOK until Gemini is implemented in `build_llm`.

## Golden pack

[`backend/app/db/seed_configs/past_analogue_golden_scenarios.json`](../backend/app/db/seed_configs/past_analogue_golden_scenarios.json)

```bash
uv run python scripts/eval_past_analogue.py list
```

Use the **same** BASE portfolio, lookback, and `past_analogue` shadow flag for every phase.

## Capture a run

1. Streamlit → enable **Past Analogue (shadow)** (or set `feature_flags.past_analogue: true`).
2. For each golden `id` / `prompt`, run optimize (Excel Copilot or API).
3. Save the `past_analogue` artifact from the run audit / `strategy_view.past_analogue` jsonb:

```bash
mkdir -p artifacts/past_analogue/baseline_ollama
# Write one file per scenario, e.g. rates_hike_50bp.json containing the artifact object
```

## Score / compare

```bash
uv run python scripts/eval_past_analogue.py score \
  --label baseline_ollama \
  --run-dir artifacts/past_analogue/baseline_ollama \
  --out artifacts/past_analogue/baseline_ollama/_summary.json

# After phase B:
uv run python scripts/eval_past_analogue.py compare \
  --run baseline_ollama=artifacts/past_analogue/baseline_ollama \
  --run plus_tavily=artifacts/past_analogue/plus_tavily \
  --out artifacts/past_analogue/compare_A_B.md
```

Optional human scores file (`human.json`):

```json
{"rates_hike_50bp": 4, "republican_wins": 5, "negative_chitchat": 5}
```

```bash
uv run python scripts/eval_past_analogue.py score \
  --label baseline_ollama \
  --run-dir artifacts/past_analogue/baseline_ollama \
  --human-scores human.json
```

## How to decide a winner

1. **expect_admit_accuracy** up (positives admit, negatives stay empty).  
2. **mean_cite_rate** up (real URLs on admitted hits).  
3. **mean_human_relevance** up on the same rubrics in the golden file.  
4. Only then glance at **mean_ΔW_L1** — useful for management “impact” demos, not for picking Tavily vs Gemini.

## Rollback

Turn off `feature_flags.past_analogue` or clear keys; eval scripts are read-only.
