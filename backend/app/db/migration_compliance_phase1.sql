-- Additive Phase 1 migration: compliance investigation corpus + chat persistence.
-- Safe to run on an existing MVP DB (IF NOT EXISTS / DROP POLICY IF EXISTS).
-- Does not alter portfolio MVP tables.
--
-- usage_metrics policy (documented choice):
--   usage_metrics remains lightweight product telemetry (PDF downloads, latency
--   counters, etc.) and is NOT mirrored into audit_events.
--   Compliance-relevant application actions (e.g. portfolio_update) write
--   directly to audit_events with source_type = 'application_event'.
--   Phase 2 agent runs will write source_type = 'agent_telemetry' via a
--   writer interface owned by the crew_ai_coding workstream.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ---------------------------------------------------------------------------
-- Immutable audit corpus (application_event | agent_telemetry)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type         TEXT NOT NULL
                        CHECK (source_type IN ('application_event', 'agent_telemetry')),
    source_name         TEXT NOT NULL,
    event_action        TEXT NOT NULL,
    event_type          TEXT,
    -- MVP: nullable so service-role writers can record events before Auth/SSO.
    -- When Auth lands, application writers SHOULD set advisor_id = auth.uid().
    -- RLS SELECT/INSERT for authenticated users scopes on advisor_id = auth.uid().
    advisor_id          UUID,
    session_id          TEXT,
    actor_id            UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    entity_refs         JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb,
    normalized_params   JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_audit_events_created_at
    ON audit_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_source_type
    ON audit_events (source_type);
CREATE INDEX IF NOT EXISTS idx_audit_events_source_name
    ON audit_events (source_name);
CREATE INDEX IF NOT EXISTS idx_audit_events_event_action
    ON audit_events (event_action);
CREATE INDEX IF NOT EXISTS idx_audit_events_session_id
    ON audit_events (session_id)
    WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_events_advisor_id
    ON audit_events (advisor_id)
    WHERE advisor_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_events_entity_refs_gin
    ON audit_events USING GIN (entity_refs);
CREATE INDEX IF NOT EXISTS idx_audit_events_normalized_params_gin
    ON audit_events USING GIN (normalized_params);

COMMENT ON TABLE audit_events IS
    'Immutable unified audit corpus. source_type: application_event | agent_telemetry. Insert-only.';
COMMENT ON COLUMN audit_events.advisor_id IS
    'RLS subject. Nullable only for MVP service-role writes until Auth lands.';
COMMENT ON COLUMN audit_events.actor_id IS
    'Auth subject / acting advisor (may equal advisor_id). Prefer over raw user_id naming.';
COMMENT ON COLUMN audit_events.entity_refs IS
    'Anonymized refs only (client_alias, portfolio_id, ticker). No raw PII.';

-- ---------------------------------------------------------------------------
-- Compliance chat persistence
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS compliance_chat_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    advisor_id      UUID NOT NULL,
    title           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_compliance_chat_sessions_advisor_id
    ON compliance_chat_sessions (advisor_id);
CREATE INDEX IF NOT EXISTS idx_compliance_chat_sessions_updated_at
    ON compliance_chat_sessions (updated_at DESC);

CREATE TABLE IF NOT EXISTS compliance_chat_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL
                    REFERENCES compliance_chat_sessions (id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_compliance_chat_messages_session_id
    ON compliance_chat_messages (session_id, created_at);

CREATE TABLE IF NOT EXISTS compliance_chat_traces (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              UUID NOT NULL
                            REFERENCES compliance_chat_sessions (id) ON DELETE CASCADE,
    message_id              UUID NOT NULL
                            REFERENCES compliance_chat_messages (id) ON DELETE CASCADE,
    raw_prompt              TEXT NOT NULL,
    normalized_query_params JSONB NOT NULL DEFAULT '{}'::jsonb,
    intermediate_steps      JSONB NOT NULL DEFAULT '[]'::jsonb,
    final_response          TEXT NOT NULL,
    citations               JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_compliance_chat_traces_session_id
    ON compliance_chat_traces (session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_compliance_chat_traces_message_id
    ON compliance_chat_traces (message_id);

COMMENT ON TABLE compliance_chat_traces IS
    'Per assistant-turn trace: prompt, params, steps, answer, citations. Insert-oriented.';

-- ---------------------------------------------------------------------------
-- Immutability: deny UPDATE/DELETE on audit_events and chat traces
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION deny_compliance_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    RAISE EXCEPTION '% is insert-only (immutable); % not allowed',
        TG_TABLE_NAME, TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_events_no_update ON audit_events;
CREATE TRIGGER trg_audit_events_no_update
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION deny_compliance_mutation();

DROP TRIGGER IF EXISTS trg_compliance_chat_traces_no_update ON compliance_chat_traces;
CREATE TRIGGER trg_compliance_chat_traces_no_update
    BEFORE UPDATE OR DELETE ON compliance_chat_traces
    FOR EACH ROW EXECUTE FUNCTION deny_compliance_mutation();

-- ---------------------------------------------------------------------------
-- RLS
-- ---------------------------------------------------------------------------
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE compliance_chat_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE compliance_chat_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE compliance_chat_traces ENABLE ROW LEVEL SECURITY;

-- audit_events: advisors see own rows; insert own rows only.
-- Service role bypasses RLS (MVP writers).
DROP POLICY IF EXISTS audit_events_select_own ON audit_events;
CREATE POLICY audit_events_select_own ON audit_events
    FOR SELECT TO authenticated
    USING (advisor_id = auth.uid());

DROP POLICY IF EXISTS audit_events_insert_own ON audit_events;
CREATE POLICY audit_events_insert_own ON audit_events
    FOR INSERT TO authenticated
    WITH CHECK (advisor_id = auth.uid());

-- No UPDATE/DELETE policies → denied for authenticated; trigger backs this up.

DROP POLICY IF EXISTS compliance_sessions_all_own ON compliance_chat_sessions;
CREATE POLICY compliance_sessions_all_own ON compliance_chat_sessions
    FOR ALL TO authenticated
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

DROP POLICY IF EXISTS compliance_messages_all_own ON compliance_chat_messages;
CREATE POLICY compliance_messages_all_own ON compliance_chat_messages
    FOR ALL TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM compliance_chat_sessions s
            WHERE s.id = session_id AND s.advisor_id = auth.uid()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM compliance_chat_sessions s
            WHERE s.id = session_id AND s.advisor_id = auth.uid()
        )
    );

DROP POLICY IF EXISTS compliance_traces_select_own ON compliance_chat_traces;
CREATE POLICY compliance_traces_select_own ON compliance_chat_traces
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM compliance_chat_sessions s
            WHERE s.id = session_id AND s.advisor_id = auth.uid()
        )
    );

DROP POLICY IF EXISTS compliance_traces_insert_own ON compliance_chat_traces;
CREATE POLICY compliance_traces_insert_own ON compliance_chat_traces
    FOR INSERT TO authenticated
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM compliance_chat_sessions s
            WHERE s.id = session_id AND s.advisor_id = auth.uid()
        )
    );
