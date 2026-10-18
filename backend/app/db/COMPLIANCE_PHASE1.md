# Compliance investigation — Phase 1

Durable audit corpus + query/cite API + Streamlit chat. **Out of scope:** full
CrewAI 5-step crew, Excel, external RAG, edits under `backend/app/agents/`.

## Schema

| Table | Role |
|-------|------|
| `audit_events` | Immutable unified corpus (`application_event` \| `agent_telemetry`) |
| `compliance_chat_sessions` | Per-advisor chat sessions |
| `compliance_chat_messages` | user / assistant / system turns |
| `compliance_chat_traces` | Prompt, params, steps, answer, citations (insert-only) |

Apply additive migration:

```bash
# Via Supabase SQL editor or psql against your project
psql "$DATABASE_URL" -f backend/app/db/migration_compliance_phase1.sql
psql "$DATABASE_URL" -f backend/app/db/seed.sql
```

Greenfield installs: `schema.sql` already includes these tables + RLS.

### RLS / immutability

- RLS enabled on all four tables; authenticated policies scope by `advisor_id = auth.uid()` (chat ownership for messages/traces via session FK).
- `audit_events.advisor_id` is **nullable for MVP service-role writers** until Auth/SSO; document and set it when Auth lands.
- Triggers `deny_compliance_mutation` block UPDATE/DELETE on `audit_events` and `compliance_chat_traces`.
- MVP API writes use the **service-role** client (bypasses RLS); user JWT still required at the FastAPI layer (`get_current_user` / `DEV_AUTH_BYPASS`).

### `usage_metrics` decision

Keep `usage_metrics` as separate product telemetry. Compliance-relevant actions
(e.g. portfolio update) write **directly** to `audit_events` with
`source_type=application_event`. No automatic dual-write.

## API (`/api/v1/compliance`)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/chat/sessions` | Create session |
| GET | `/chat/sessions` | List sessions |
| GET | `/chat/sessions/{id}` | Messages + traces |
| POST | `/chat/sessions/{id}/messages` | Deterministic pipeline + persist |
| GET | `/chat/sessions/{id}/export` | JSON (+ markdown) export |
| POST | `/query` | Direct query+cite (no chat) |
| GET | `/audit-events` | Filtered list |
| GET | `/audit-events/{id}` | Single event |

## Phase-1 pipeline (deterministic)

Mirrors Intake → Plan → Query → Reason → Cite in
`backend/app/services/compliance_chat.py` without CrewAI. Policy sources of
truth: `trading_rules` + `configs` only.

## Phase-2 agent telemetry (handoff)

Interface: `AgentTelemetryWriterContract` in
`backend/app/services/audit_events.py`. Implementation owned by **crew_ai_coding**;
must not be implemented by editing agents from this workstream.

## Manual test checklist

1. [ ] Migration applied; sample seed rows present
2. [ ] `POST /portfolios/validate-import` (or build-base) still succeeds; new `audit_events` row appears
3. [ ] Streamlit chat answers for seed log `aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeee0001`
4. [ ] Citations include `audit_event_id` and `rule_id` and/or `config_key`+`version`
5. [ ] Export downloads JSON with messages + traces
6. [ ] No secrets in traces; prompts anonymized
