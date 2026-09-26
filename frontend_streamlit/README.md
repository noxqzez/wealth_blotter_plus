# Compliance investigation chat (Phase 1)

Streamlit ops UI for querying the unified `audit_events` corpus and citing
`trading_rules` / versioned `configs`. **No Excel work in this surface.**

```bash
# API must be running (DEV_AUTH_BYPASS=true for local demos without JWT)
uv run uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000

# Optional: set Bearer JWT if bypass is off
export WBP_API_BASE=http://localhost:8000/api/v1
# export WBP_API_JWT=eyJ...

uv run streamlit run frontend_streamlit/app.py
```

Do not put service-role keys in Streamlit client secrets for browser exposure.
Prefer calling FastAPI with the user JWT, or server-side secrets only in
`.streamlit/secrets.toml` (gitignored).

## Phase 1 demo checklist

1. Apply DDL: `backend/app/db/migration_compliance_phase1.sql` (or full `schema.sql`)
2. Seed: `backend/app/db/seed.sql` (sample events with fixed IDs below)
3. Open **Compliance chat** → **New session**
4. Ask: `Does log ID aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeee0001 violate max single-name weight?`
5. Expand **Sources & Audit Logs Used** — expect `audit_event_id` + `trading_rules` / `configs`
6. **Export session audit trace** → download JSON (and markdown)

### Example queries

| Query | What Phase 1 does |
|-------|-------------------|
| `Does log ID aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeee0001 violate …?` | Lookup by id → cite weight vs `max_single_name_weight` / guardrails |
| `Why was yesterday's portfolio_update flagged?` | Time + action filter → list events + policy notes |
| `Show application_event for ticker AAPL` | Entity ref filter on `entity_refs.ticker` |

### Insert sample `application_event` rows

```sql
INSERT INTO audit_events (
  source_type, source_name, event_action, event_type,
  advisor_id, actor_id, entity_refs, payload, normalized_params
) VALUES (
  'application_event',
  'portfolio_api_v1',
  'portfolio_update',
  'portfolio',
  '00000000-0000-4000-8000-000000000001',
  '00000000-0000-4000-8000-000000000001',
  '{"client_alias": "demo_client_a", "ticker": "AAPL"}'::jsonb,
  '{"positions_count": 1, "row_count": 1, "ticker_count": 1,
    "positions": [{"ticker": "AAPL", "current_weight_pct": 0.72}]}'::jsonb,
  '{"event_action": "portfolio_update"}'::jsonb
);
```

Live portfolio updates also emit via `POST /api/v1/portfolio/update`
(`source_name=portfolio_api_v1`). Emission failures are logged and do not
block the blotter write.

### Fixed seed log IDs

- `aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeee0001` — concentrated AAPL weight (yesterday)
- `aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeee0002` — crypto-heavy update (recent)
- `aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeee0003` — `agent_telemetry` placeholder

## Phase 2 handoff (crew_ai_coding)

Do **not** edit `backend/app/agents/` from this workstream. Agent runs should
call the documented writer contract:

- Module: `backend.app.services.audit_events.AgentTelemetryWriterContract`
- `source_type = "agent_telemetry"`, `source_name = "agent_trace_run"`
- Always anonymize before persist; never log secrets

## `usage_metrics` vs `audit_events`

`usage_metrics` stays lightweight product telemetry (downloads, latency).
Compliance investigation uses **only** `audit_events`. Do not dual-write
unless a future product decision maps specific usage actions into the corpus.
