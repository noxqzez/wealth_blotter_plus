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
uv sync

# 2. API
uv run uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000

# 3. Excel task pane (HTTPS required)
cd frontend_excel && pnpm install && pnpm dev

# 4. Streamlit (optional)
uv run streamlit run frontend_streamlit/app.py
```

Local agents need [Ollama](https://ollama.com) running (`ollama pull llama3.1`).

## Layout

```text
backend/app/          FastAPI, schemas, agents, SQL
frontend_excel/       Office.js task pane (Vite + mkcert)
frontend_streamlit/   Ops & agent debug dashboard
rules/                Human-readable Cursor rules
```

## Agent & security docs

Start with [AGENTS.md](AGENTS.md) and [`.cursorrules`](.cursorrules). Put secrets only in `.env` (gitignored).
