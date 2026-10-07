# Streamlit ops UI

Two surfaces in `app.py`:

1. **Usage & Control Monitor** (managers) — adoption KPIs, estimated unit economics,
   HITL / trading-rule risk view. Uses Supabase **service role** when configured;
   otherwise realistic mock data.
2. **Compliance chat** — Phase 1 investigation over `audit_events`.

```bash
# API (for compliance chat)
uv run uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000

# Exec monitor — local manager bypass + mock data (no Supabase required)
export DEV_MANAGER_BYPASS=true
uv run streamlit run frontend_streamlit/app.py
```

Prefer `uv run` (project `.venv`). Anaconda’s system `pyarrow` is often broken
(brotli ABI mismatch); this app avoids `st.dataframe` / Altair for that reason.

Mock unit economics use **8 bps fee capture on trade notional** (not gross notional)
so Revenue / cost lands in a marketing-viable ~12–25× band. Live telemetry will
replace estimated token costs; fee bps can later come from real commission tables.



## Manager access

| Mode | Config |
|------|--------|
| Local demo | `DEV_MANAGER_BYPASS=true` |
| Allowlist | `MANAGER_USER_IDS` and/or `MANAGER_EMAILS` (comma-separated) in env or `.streamlit/secrets.toml` |
| Live data | `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` (server-side only — never ship to browsers) |

Optional identity display: `WBP_MANAGER_USER_ID` / `WBP_MANAGER_EMAIL` matching the allowlist.

## What the monitor reads

| KPI / view | Source |
|------------|--------|
| Active advisors / runs | `scenario_runs` |
| Staged order value | `staged_trades.notional_value` |
| Automation rate | Inverse of `scenario_param_diffs` edits + `portfolios.import_hitl` |
| Satisfaction | `interaction_ratings` |
| Rule rejects | `staged_trades.metadata` (or mock `rule_status`) |
| Token spend | **Estimated** until usage telemetry exists (`usage_metrics` ready) |

Sidebar cost controls (`coin_temperature`, token cap, open-LLM route) are **UI-only** for now.

## Compliance chat

See prior Phase 1 notes below. Prefer FastAPI + user JWT for chat; do not put
service-role keys in client-exposed config.

```bash
export WBP_API_BASE=http://localhost:8000/api/v1
# export WBP_API_JWT=eyJ...
uv run streamlit run frontend_streamlit/app.py
```
