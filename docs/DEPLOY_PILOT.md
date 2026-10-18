# Pilot production deploy checklist

**ALWAYS deploy the API / Excel static site from Git branch `main`.**  
Do **not** deploy from `develop/next` (or other experiment branches) unless you
explicitly intend a test-only preview.

Known-good snapshot reference: commit that introduced Past Analogue + dual BYOK
+ RAG upload guards (see `git log` on `main`). Prefer tagging before cutover,
e.g. `git tag pilot-v1` on the commit you ship.

---

## Pre-deploy security gates (must be true on Render)

| Check | Required production value | Why |
|-------|---------------------------|-----|
| Deploy source | Git branch **`main`** | Experiment work stays on `develop/next` |
| `ENVIRONMENT` | `production` | Disables OpenAPI `/docs` |
| `DEV_AUTH_BYPASS` | `false` | Never allow unauthenticated API use |
| `REQUIRE_PILOT_APPROVAL` | `true` | Only `APPROVED` profiles |
| `REQUIRE_BYOK` | `true` (recommended) | Pilots must send primary LLM headers |
| `CORS_ORIGINS` | Exact Excel Static Site + local debug origins only | No `*` |
| `SUPABASE_SERVICE_ROLE_KEY` | Set on API only | Never in Excel / Streamlit browser |
| `ADMIN_API_KEY` | Long random; `/admin/pilots` only | Host approve + welcome email |
| Storage bucket `research-documents` | **Private** | RAG PDFs |
| RAG uploads | `RAG_UPLOAD_ENABLED=true` (or `false` to freeze) | Kill switch |
| RAG quotas | `RAG_UPLOAD_RATE_LIMIT_PER_MINUTE=5`, `RAG_UPLOAD_MAX_PAGES_PER_DAY=50`, `RAG_UPLOAD_MAX_PAGES_PER_FILE=50` | Abuse cap |
| Migrations | Pilot profiles + RAG POC + `migration_rag_upload_guards.sql` applied | Quotas need `page_count` |

---

## Observability / ops smoke after deploy

1. `GET /api/v1/health` → 200  
2. Request-access page loads; approve one test pilot via `/admin/pilots`  
3. Excel sideload `manifest.pilot.xml` → primary LLM → sign in  
4. Import securities → scenario parse → run → `ord_blotter` + PDF  
5. Confirm `usage_metrics` / `audit_events` rows for that `advisor_id`  
6. If Past Analogue on: chat shows `weight input from historical events = …` and jsonb under `scenario_runs.strategy_view.past_analogue`

---

## Concurrency / data-loss notes (pilot expectations)

- Copilot chat FSM is **in-memory** — Render restart/redeploy clears mid-chat sessions (user reopens Copilot). Portfolio / scenario / audit data in Supabase is durable.
- Single Render web instance is assumed for Copilot session affinity; do not scale to N instances without a shared session store.
- Past Analogue / scenario failures should fail-open where designed; live blotter still depends on successful optimize path.

---

## Related docs

- [PILOT_PHASE1.md](PILOT_PHASE1.md) — host env + Render wiring  
- [PILOT.md](../PILOT.md) — what pilots do  
- [RAG_POC.md](../backend/app/db/RAG_POC.md) — upload guards  
- [PAST_ANALOGUE_EVAL.md](PAST_ANALOGUE_EVAL.md) — historical match eval  
