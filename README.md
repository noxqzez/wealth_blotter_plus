# Wealth Blotter Plus

Institutional wealth advisory platform: FastAPI + CrewAI + Supabase, with an Office.js Excel task pane and Streamlit ops dashboard.

## Stack

- **API:** FastAPI (Python 3.10+), Pydantic v2, Uvicorn
- **Agents:** CrewAI (Ollama local-first; cloud LLMs via env flags)
- **DB:** Supabase PostgreSQL + RLS
- **Clients:** `frontend_excel/` (Vite + Office.js), `frontend_streamlit/` (ops / debug)

## Quick start

```bash
# 1. Python deps (uv)
cp .env.example .env   # fill Supabase + JWT secrets locally — never commit
# For local Excel MVP without SSO:
# DEV_AUTH_BYPASS=true
uv sync

# 2. API
uv run uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000

# 3. Excel task pane (HTTPS — Mac needs a trusted local cert once)
brew install mkcert
mkcert -install          # enter Mac password when prompted
cd frontend_excel
npm install
npm run certs            # writes frontend_excel/.certs/
npm run dev              # https://localhost:3000
# Sideload frontend_excel/manifest.xml
```

### MVP flow (WF-01 import)

1. Paste `client_alias | ticker | quantity | asset_class | currency` into Excel (header + rows; currency optional)
2. Select the range → **UPDATE PORTFOLIO**
3. API: `validate-import` (normalize + draft) → `build-base` (Yahoo enrich + PosIDs)
4. **`portfolio_scratchpad`** sheet shows the final base blotter (spot, market value, weight, status, pos_id)

Apply `backend/app/db/migration_portfolio_import_wf01.sql` (or refreshed `schema.sql`) before first run.

## Layout

```text
backend/app/          FastAPI, schemas, agents, SQL
frontend_excel/       Office.js task pane (Vite + mkcert)
frontend_streamlit/   Ops, agent debug, compliance chat
rules/                Human-readable Cursor rules
```

### Compliance investigation (Phase 1)

Query/cite `audit_events` against `trading_rules` + `configs` via Streamlit:

```bash
# After applying backend/app/db/migration_compliance_phase1.sql + seed.sql
DEV_AUTH_BYPASS=true uv run uvicorn backend.app.main:app --reload --port 8000
uv run streamlit run frontend_streamlit/app.py
```

See [backend/app/db/COMPLIANCE_PHASE1.md](backend/app/db/COMPLIANCE_PHASE1.md) and
[frontend_streamlit/README.md](frontend_streamlit/README.md).

### Research RAG (POC)

Upload research PDFs → classify (US/HK/Crypto/Neither) → Hugging Face embed →
`pgvector` retrieve. Uploads are **per `advisor_id`** (not shared across pilots).

```bash
# Supabase: enable vector + create private bucket research-documents
# Apply backend/app/db/migration_rag_poc.sql
# Apply backend/app/db/migration_rag_upload_guards.sql  # page_count + daily quota
uv sync --extra rag
# POST /api/v1/documents/upload | /jobs/{id} | /retrieve
```

**Pilot upload guards (option B):** kill switch `RAG_UPLOAD_ENABLED`, rate limit
(default 5/min), **50 PDF pages/day** and **50 pages/file** (UTC). Details:
[backend/app/db/RAG_POC.md](backend/app/db/RAG_POC.md).

### Past Analogue (historical match)

Opt-in scenario step (`feature_flags.past_analogue`): Tavily (or public fallback)
+ Ollama extract → admit date windows → **50/50 blend** of strategist Opti Folio
with analogue-conditioned weights (trading rules still enforced). Lineage lives in
`scenario_runs.strategy_view.past_analogue` and `audit_events` (`past_analogue_agent`).

```bash
# .env: TAVILY_API_KEY=…  (optional; unset = baseline public search)
# Ollama must be running for extract
# Streamlit: toggle “Enable Past Analogue”
uv run python scripts/eval_past_analogue.py list
```

Eval / A-B pack: [docs/PAST_ANALOGUE_EVAL.md](docs/PAST_ANALOGUE_EVAL.md).

## What’s in this snapshot (recent)

- Dual LLM BYOK (primary + optional secondary) in Excel / API headers
- Past Analogue search → admit → apply with mix; chat one-liner + jsonb audit
- Scenario talking points: market color preferred over canned house summary
- RAG upload kill switch / rate limit / 50 pages/day
- Pilot welcome email + request-access / admin pilots flow
- Horizon parsing for “next 12 months” → `1Y`; ord_blotter BUY/SELL colors

## Pilot users

**Share with pilots**

1. Signup: `https://wealth-blotter-plus.onrender.com/request-access` (or your API Render URL)
2. Manifest: `https://wealth-blotter-plus-excel.onrender.com/manifest.pilot.xml` (or your Static Site URL)
3. Tell them to follow **[PILOT.md](PILOT.md)**  
   (request access → approval → sideload manifest once → primary LLM + optional secondary → sign in in Excel)

Host/operator Render + Supabase checklist: [docs/PILOT_PHASE1.md](docs/PILOT_PHASE1.md).  
Approve + welcome email UI: `https://<api>.onrender.com/admin/pilots` (needs `ADMIN_API_KEY` + Resend).

## Agent & security docs

Start with [AGENTS.md](AGENTS.md) and [`.cursorrules`](.cursorrules). Put secrets only in `.env` (gitignored).
