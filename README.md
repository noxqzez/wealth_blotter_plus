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
`pgvector` retrieve:

```bash
# Supabase: enable vector + create private bucket research-documents
# Apply backend/app/db/migration_rag_poc.sql
uv sync --extra rag
# POST /api/v1/documents/upload | /jobs/{id} | /retrieve
```

See [backend/app/db/RAG_POC.md](backend/app/db/RAG_POC.md).

## Pilot users

**Share with pilots**

1. Signup: `https://wealth-blotter-plus.onrender.com/request-access` (or your API Render URL)
2. Manifest: `https://wealth-blotter-plus-excel.onrender.com/manifest.pilot.xml` (or your Static Site URL)
3. Tell them to follow **[PILOT.md](PILOT.md)**  
   (request access → approval → sideload manifest once → BYOK + sign in in Excel)

Host/operator Render + Supabase checklist: [docs/PILOT_PHASE1.md](docs/PILOT_PHASE1.md).

## Agent & security docs

Start with [AGENTS.md](AGENTS.md) and [`.cursorrules`](.cursorrules). Put secrets only in `.env` (gitignored).
