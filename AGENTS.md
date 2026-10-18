# Wealth Blotter Plus — Agent Rules

Project root: repository checkout of `wealth_blotter_plus`

Cursor also loads machine-readable copies from `.cursor/rules/` (hidden).
**Visible copies for humans:** see the `rules/` folder in this project.
Root coding standards also live in [`.cursorrules`](.cursorrules).

## Always-on rules

- [Workspace Scope](rules/01_workspace_scope.md)
- [Core Stack & Architecture](rules/02_wealth_blotter_core.md)
- [Security & Secrets](rules/03_security_secrets.md)
- [Pilot deploy](rules/07_deploy_pilot.md) — production from **`main` only**; see [docs/DEPLOY_PILOT.md](docs/DEPLOY_PILOT.md)

## Language-specific rules

- [Python Standards](rules/04_python_standards.md)
- [SQL Standards](rules/05_sql_standards.md)
- [Frontend Standards](rules/06_frontend_standards.md)

---

## Parallel coding agent boundaries

### Directory ownership (do not cross without explicit handoff)

| Area | Path | Owns |
|------|------|------|
| API & core | `backend/app/main.py`, `backend/app/config.py`, `backend/app/core/` | Settings, JWT, Supabase clients, rules cache |
| HTTP surface | `backend/app/api/` | FastAPI routers only |
| Contracts | `backend/app/schemas/` | Pydantic models (immutable until agreed) |
| Agents | `backend/app/agents/` | CrewAI, tools, YAML, self-healing |
| Database | `backend/app/db/` | SQL DDL, RLS, seeds, seed JSON configs |
| Excel add-in | `frontend_excel/` | Vite / Office.js task pane |
| Ops UI | `frontend_streamlit/` | Streamlit admin & agent debug |

**Rule:** One parallel agent owns one directory tree at a time. Do not edit files outside your assigned scope.

### Interface-first contracts

- Treat Pydantic models, FastAPI OpenAPI schemas, and SQL DDL as **immutable contracts**.
- Prefer extending schemas with optional fields over breaking renames.
- Frontend and agent workers code against `backend/app/schemas/` and documented API routes.

### Type safety & Python

- Python 3.10+, strict typing, Pydantic v2, async-first I/O.
- Structured logging (`structlog` / JSON). No bare `except:`.
- Deterministic logic lives in tools — never use an LLM for caps, regex, or hard thresholds.

### Auth & secrets

- User calls: Supabase Auth JWT (`Authorization: Bearer …`); validate in `security.py`.
- Admin/audit/config writes: service-role client from server `.env` only.
- Never log, commit, or return `SUPABASE_SERVICE_ROLE_KEY`, JWT secrets, or `advisor_credentials` plaintext.

### Agent execution

- Global LLM `temperature = 0.2` (override per agent in YAML).
- All agent outputs bind to Pydantic models with `confidence_score` ∈ [0.0, 1.0].
- Self-heal validation failures up to **3** retries.
- `confidence_score >= 0.85` → blotter staging; `< 0.85` → HITL review.
- Persist agent output vs advisor-saved output for accuracy audit.
- Anonymize names and account numbers before any LLM payload (esp. cloud).

### Rules-as-code

- Canonical: Supabase `configs` table (`jsonb`).
- Seeds: `backend/app/db/seed_configs/*.json` + `seed.sql`.
- Never store rules in Storage buckets.
