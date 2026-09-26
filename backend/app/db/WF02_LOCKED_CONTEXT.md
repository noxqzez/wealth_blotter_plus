# WF-02 Market Scenarios — Locked Context

**Status:** Contract locked. Waves A–E landed.  
**Last updated:** 2026-09-21  
**Do not re-ask clarifications** — answers below are final unless the user changes them.

### Progress log

| When | What |
|------|------|
| 2026-09-18 | Locked Q1–Q11; deps; Wave A schemas/migration/seeds |
| 2026-09-20 | Wave B services |
| 2026-09-20 | Wave C: parse + stub strategy |
| 2026-09-21 | Wave D: `/api/v1/scenarios` |
| 2026-09-21 | Wave E: Excel prompt, Scenario sheet, Run, `tgt_folio`, PDF |

**Still TODO:** Excel UI polish; PDF template; validation gates; compliance Streamlit inspector by PosID/client/portfolio/scenario.  
**2026-09-26:** `portfolios.import_hitl` — boolean KPI when import needed Reprocess / Drop Invalid (garbage→folio HITL rate).
**2026-09-26:** Place All Orders end-state: `staged_trades.status` Pending→Executed; `scenario_runs.orders_hitl` when Excel blotter ≠ staged.  
**2026-09-25:** House (RAG) + market (Yahoo news) research **activated**; strategist uses in-house-priority merge (`strategist_stub_ok: false`).

### Trace opti_folio lineage (audit_events)

Primary key for a workflow: **`scenario_runs.id`** (= `scenario_run_id`; Excel Scenario sheet **B1** after Parse).

```sql
-- Latest runs (id IS the scenario_run_id)
SELECT
  id AS scenario_run_id,
  status,
  created_at,
  left(user_prompt, 80) AS prompt,
  portfolio_id
FROM scenario_runs
ORDER BY created_at DESC
LIMIT 20;
```

```sql
-- Full hop bus for one scenario run (ordered)
SELECT
  created_at,
  event_action,
  source_type,
  source_name,
  payload->>'step' AS step,
  (payload->>'step_order')::int AS step_order,
  payload->'agent_or_component' AS agent,
  payload->'implementation' AS implementation,
  payload->'input' AS input,
  payload->'output' AS output,
  payload->'messages' AS messages,
  id AS audit_event_id
FROM audit_events
WHERE session_id = 'YOUR-SCENARIO-RUN-UUID'
   OR entity_refs->>'scenario_run_id' = 'YOUR-SCENARIO-RUN-UUID'
ORDER BY (payload->>'step_order')::int NULLS LAST, created_at;
```

```sql
-- All runs for a portfolio, then drill into session_id
SELECT id, status, created_at, user_prompt
FROM scenario_runs
WHERE portfolio_id = 'YOUR-PORTFOLIO-UUID'
ORDER BY created_at DESC;
```

Hops include: parse, human params, market history, house research (RAG), market research (Yahoo news), strategist (in-house priority merge), risk base, quant optimizer, compliance, risk opti, content, run complete/failed.

### Compliance query use-cases (future)

Investigators will look up **end-to-end message exchange** using any of:
- `scenario_run_id` / scenario ID → `audit_events.session_id`
- `portfolio_id` → `entity_refs.portfolio_id` or via `scenario_runs`
- `client_id` / `client_alias` → `entity_refs`
- PosID (`portfolio_positions.id`) → join positions → portfolio → scenario runs → audit hops

Preferred presentation: **sequential hop JSON** (step_order, agent, input, output) as shown in UAT review — same layout as the audit handoff trail.



---

## Excel UX (user-facing only)

1. Input natural-language scenario prompt  
2. Validate / edit structured `scenario_params` (approve or edit)  
3. Download formatted PDF at the end  

Research / optimize are **not** Excel buttons — they run inside backend `POST …/run`.  
Implied Excel actions: **Parse** → **Update params** (optional) → **Run scenario** → **Download PDF**.

---

## Locked decisions (Q1–Q11)

| # | Topic | Decision |
|---|--------|----------|
| 1 | API / Excel | Agents/APIs are triggers. Excel: prompt → validate/edit params → PDF. `/run` = full pipeline; research/optimize internal. |
| 2 | Asset class | Always **`CRYPTO`** (never `"Coins"`). |
| 3 | Agent vs deterministic | Param accuracy = **deterministic diffs** (agent params vs human edit). Market research Phase 1 = broadly available news OK. Risk/BL/cvxpy/compliance/PDF = **Python only** (no LLM math). |
| 4 | Param diffs | Human edits only → `scenario_param_diffs` + `is_exact_match`. Perfect accuracy = zero edits. |
| 5 | Phasing | Phase 1 UAT first; must be **easily extensible to Phase 2** (`phase`, `feature_flags` in `configs.scenario_wf02`). |
| 6 | Coin universe | Store approved list internally. **Only** these 5: `BTC`, `ETH`, `DOGE`, `XRP`, `SOL`. Optimizer may **add** approved coins to existing base folio to improve metrics — **no other coins**. |
| 7 | Client rules | Firm-wide. Rule text form: “client_xxx cannot trade yyy” → `CLIENT_RESTRICTION` with client in `parameters`. |
| 8 | BASE required | Run requires WF-01 `folio_stage=BASE`. |
| 9 | LLM | **Ollama for everything in UAT.** Published/prod: users supply a separate LLM key for external research; **recommend Claude Sonnet** for that path (noted in `.env.example`). |
| 10 | Timeouts | Reuse WF-01 pattern with longer run: parse ~30s, run ~120s, PDF ~30s. Codes: `WBP-SCENARIO-*`. |
| 11 | Dependencies | Added before coding: numpy, pandas, scipy, scikit-learn, cvxpy, reportlab (`pyproject.toml` + `uv sync`). |

---

## Trading rules (one table)

**Yes — all position / weight / universe / jurisdiction rules in `trading_rules`.**  
`business_guardrails` keeps only soft knobs: `coin_temperature`, `confidence_threshold`.

| Category | Example rows / `parameters` |
|----------|---------------------|
| `PORTFOLIO` | `max_single_name_weight` `{max_single_weight:0.25}` (keep `max_value` in sync) · `min_cash_buffer` `{min_cash_weight:0.05}` · `approved_crypto_universe` `{approved_tickers:[…]}` · optional `asset_class_weight_cap` |
| `TRADE` | `{"restricted_tickers":["SHIB-USD"]}` |
| `CLIENT` | `jurisdictions` `{allowed_markets:[…]}` · `client_2_no_coins` `{client_alias, ineligible_tickers}` |
| `TECHNICAL` | *(none in trading_rules — use `configs.technical_limits`)* |

- Engine reads `is_active = true` only.  
- **One row per policy name** (unique `rule_name`) — never duplicate single-name caps.  
- Samples: `seed_trading_rules_examples.sql` + `seed.sql`.

## Soft product knobs (`configs.business_guardrails`)

- `coin_temperature`: integer dial **1–5** (default **2**; **3 = neutral**). Scales CRYPTO BL tilts / convictions. Owned by management (Streamlit dashboard); **not** editable on the Excel Scenario sheet.
- `confidence_threshold`: HITL routing threshold (also mirrored in settings)

---

## Phase 1 vs Phase 2

**Phase 1 (ship first):**  
Parse → Scenario tab → Update params + diffs → Risk on base_folio (3Y Yahoo) → Stub/house strategy_view → Deterministic optimize + compliance loop → tgt folio + staged_trades → PDF.

**Phase 2 (flags off initially):**  
Market research web + external LLM (Claude) + richer content.  
Config: `configs.scenario_wf02` / `seed_configs/scenario_wf02.json` (`feature_flags.market_research_web`, `external_research_llm`, `strategist_stub_ok`).

---

## Design split (immutable)

| Step | LLM? | Deterministic? |
|------|------|----------------|
| NL → `scenario_params` | Yes (`scenario_agent`) | — |
| Param edit accuracy | — | Diffs |
| House research | Yes + RAG | — |
| Market research | Yes (Phase 2 web; Phase 1 broad news OK) | — |
| Strategist | Yes | Hierarchy: in-house wins on conflict |
| Risk / cov / BL / cvxpy | **No** | **Yes** |
| Compliance re-solve | No | Yes |
| Talking points | Yes | — |
| PDF layout | No | Yes (reportlab) |

---

## API shape (Excel-facing)

- `POST /api/v1/scenarios/parse` — prompt → `scenario_params`  
- `PUT /api/v1/scenarios/params` — human edit + write diffs  
- `POST /api/v1/scenarios/run` — full pipeline (research ∥ risk → BL/optimize → stage → content)  
- `POST /api/v1/scenarios/pdf` (or GET) — download PDF  

Internal/debug only: research, optimize endpoints if added later.

Timeouts (from `scenario_wf02`): parse 30s / params 15s / run 120s / pdf 30s.

Error codes: `WBP-SCENARIO-TIMEOUT|PARSE|PARAMS|RUN|PDF|SERVER|NETWORK|CLIENT|BASE-REQUIRED|COMPLIANCE`.

---

## DB objects (Wave A)

Tables (see `schema.sql` + `migration_scenario_wf02.sql`):

- `scenario_runs` — lifecycle, `agent_scenario_params`, `scenario_params`, artifacts jsonb, `phase`, `feature_flags`  
- `scenario_param_diffs` — field-level human vs agent  
- `staged_trades` — restored; **`order_id` UNIQUE** citation id  

Apply when ready: `backend/app/db/migration_scenario_wf02.sql` in Supabase SQL Editor.

Pydantic: `backend/app/schemas/scenario.py` (use `scenario_run_id`, not `scenario_id`).

---

## Waves (one directory tree at a time)

| Wave | Owner | Status |
|------|--------|--------|
| **A** | `schemas/` + `db/` | **Done** |
| **B** | `services/` | **Done** |
| **C** | `agents/` | **Partial** — parse + stub strategy; house/market/content still thin |
| **D** | `api/` | **Done** — see endpoints below |
| **E** | `frontend_excel/` | **Done** — `src/scenario.ts` + task pane buttons |

**Wave E Excel**
- Portfolio id filled after UPDATE PORTFOLIO
- Parse → `Scenario` sheet (editable fields)
- Update parameters (optional) → accuracy diffs
- Run scenario → `tgt_folio` (positions, metrics, trades, talking points)
- Download PDF

**Before live UAT:** apply `backend/app/db/migration_scenario_wf02.sql` in Supabase. API + Excel must be running (Vite proxy `/api` → `:8000`). Ollama required for Parse.

---

## Resume prompt (copy-paste)

```
WF-02 Waves A–E are implemented. See backend/app/db/WF02_LOCKED_CONTEXT.md.
Do not rebuild them. Next work only if asked: apply migration, UAT bugs, or Phase-2 research agents.
```

---

## Explicitly out of scope until asked

- Committing / PR  
- Applying Supabase migration without user  
- Full Wave B–E in one shot  
- Managing weight caps via `trading_rules` PORTFOLIO rows (single-name + optional asset-class sleeve)
