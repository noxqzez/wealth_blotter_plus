-- Wealth Blotter Plus — PostgreSQL / Supabase schema (scaffold)
-- Apply via Supabase SQL editor or migration tooling when ready.
-- v1 RLS: advisor_id = auth.uid()
-- firm_id present for future multi-advisor firm isolation (not enforced yet)

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ---------------------------------------------------------------------------
-- Firms (schema readiness for multi-tenant firms)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS firms (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Advisors (1:1 with auth.users for v1)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS advisors (
    id          UUID PRIMARY KEY REFERENCES auth.users (id) ON DELETE CASCADE,
    firm_id     UUID REFERENCES firms (id) ON DELETE SET NULL,
    display_name TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Clients (PII stays in DB; never send raw names to external LLMs)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clients (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    advisor_id      UUID NOT NULL REFERENCES advisors (id) ON DELETE CASCADE,
    firm_id         UUID REFERENCES firms (id) ON DELETE SET NULL,
    display_name    TEXT,
    account_number_encrypted TEXT,
    external_ref    TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (advisor_id, external_ref)
);

CREATE INDEX IF NOT EXISTS idx_clients_advisor_id ON clients (advisor_id);

-- ---------------------------------------------------------------------------
-- Portfolios
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portfolios (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    advisor_id  UUID NOT NULL REFERENCES advisors (id) ON DELETE CASCADE,
    firm_id     UUID REFERENCES firms (id) ON DELETE SET NULL,
    client_id   UUID NOT NULL REFERENCES clients (id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    base_ccy    TEXT NOT NULL DEFAULT 'USD',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_portfolios_advisor_id ON portfolios (advisor_id);

-- ---------------------------------------------------------------------------
-- Order blotter (no real OMS — mock simulator later)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staged_trades (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    advisor_id      UUID NOT NULL REFERENCES advisors (id) ON DELETE CASCADE,
    firm_id         UUID REFERENCES firms (id) ON DELETE SET NULL,
    portfolio_id    UUID NOT NULL REFERENCES portfolios (id) ON DELETE CASCADE,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity        NUMERIC NOT NULL CHECK (quantity > 0),
    limit_price     NUMERIC,
    asset_class     TEXT,
    status          TEXT NOT NULL DEFAULT 'staged'
                    CHECK (status IN ('staged', 'hitl', 'approved', 'simulated_sent', 'cancelled')),
    confidence_score NUMERIC NOT NULL CHECK (confidence_score >= 0 AND confidence_score <= 1),
    agent_run_id    UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_staged_trades_advisor_id ON staged_trades (advisor_id);
CREATE INDEX IF NOT EXISTS idx_staged_trades_status ON staged_trades (status);

-- ---------------------------------------------------------------------------
-- Rules-as-code (canonical jsonb — not Storage buckets)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS configs (
    key         VARCHAR(128) PRIMARY KEY,
    config      JSONB NOT NULL,
    version     INT NOT NULL DEFAULT 1,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  UUID REFERENCES auth.users (id)
);

-- ---------------------------------------------------------------------------
-- Immutable audit trail (service-role writes)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    advisor_id      UUID,
    firm_id         UUID,
    event_type      TEXT NOT NULL,
    request_path    TEXT,
    query_params    JSONB,
    latency_ms      NUMERIC,
    confidence_score NUMERIC,
    validation_ok   BOOLEAN,
    status          TEXT,
    detail          JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_advisor_id ON audit_logs (advisor_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs (created_at);

-- Prevent updates/deletes on audit_logs (immutability best-effort)
CREATE OR REPLACE FUNCTION deny_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_logs_no_update ON audit_logs;
CREATE TRIGGER trg_audit_logs_no_update
    BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION deny_audit_mutation();

-- ---------------------------------------------------------------------------
-- Advisor third-party API credentials (encrypted at rest)
-- Prefer Supabase Vault (vault.secrets) in production; pgcrypto placeholder below.
-- NEVER select plaintext into API responses.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS advisor_credentials (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    advisor_id      UUID NOT NULL REFERENCES advisors (id) ON DELETE CASCADE,
    firm_id         UUID REFERENCES firms (id) ON DELETE SET NULL,
    provider        TEXT NOT NULL,
    vault_secret_id UUID,
    -- Fallback encrypted payload if Vault not yet wired (app encrypts before insert)
    secret_ciphertext BYTEA,
    meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (advisor_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_advisor_credentials_advisor_id ON advisor_credentials (advisor_id);

-- ---------------------------------------------------------------------------
-- App usage metadata
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_usage_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    advisor_id      UUID REFERENCES advisors (id) ON DELETE SET NULL,
    firm_id         UUID REFERENCES firms (id) ON DELETE SET NULL,
    event_name      TEXT NOT NULL,
    surface         TEXT NOT NULL CHECK (surface IN ('excel', 'streamlit', 'api', 'agent')),
    properties      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_app_usage_events_advisor_id ON app_usage_events (advisor_id);

-- ---------------------------------------------------------------------------
-- Agent vs advisor accuracy audit (HITL corrections)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_output_reviews (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    advisor_id              UUID NOT NULL REFERENCES advisors (id) ON DELETE CASCADE,
    firm_id                 UUID REFERENCES firms (id) ON DELETE SET NULL,
    portfolio_id            UUID REFERENCES portfolios (id) ON DELETE SET NULL,
    run_id                  UUID NOT NULL,
    agent_output            JSONB NOT NULL,
    advisor_saved_output    JSONB NOT NULL,
    confidence_score        NUMERIC,
    notes                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_output_reviews_advisor_id ON agent_output_reviews (advisor_id);
CREATE INDEX IF NOT EXISTS idx_agent_output_reviews_run_id ON agent_output_reviews (run_id);

-- ---------------------------------------------------------------------------
-- Row Level Security (v1: advisor isolation)
-- ---------------------------------------------------------------------------
ALTER TABLE advisors ENABLE ROW LEVEL SECURITY;
ALTER TABLE clients ENABLE ROW LEVEL SECURITY;
ALTER TABLE portfolios ENABLE ROW LEVEL SECURITY;
ALTER TABLE staged_trades ENABLE ROW LEVEL SECURITY;
ALTER TABLE advisor_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_usage_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_output_reviews ENABLE ROW LEVEL SECURITY;

-- configs & audit_logs: service role / controlled access (no broad authenticated ALL)
ALTER TABLE configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE firms ENABLE ROW LEVEL SECURITY;

-- Advisors: self row only
DROP POLICY IF EXISTS advisors_self ON advisors;
CREATE POLICY advisors_self ON advisors
    FOR ALL
    USING (id = auth.uid())
    WITH CHECK (id = auth.uid());

DROP POLICY IF EXISTS clients_advisor_isolation ON clients;
CREATE POLICY clients_advisor_isolation ON clients
    FOR ALL
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

DROP POLICY IF EXISTS portfolios_advisor_isolation ON portfolios;
CREATE POLICY portfolios_advisor_isolation ON portfolios
    FOR ALL
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

DROP POLICY IF EXISTS staged_trades_advisor_isolation ON staged_trades;
CREATE POLICY staged_trades_advisor_isolation ON staged_trades
    FOR ALL
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

DROP POLICY IF EXISTS advisor_credentials_isolation ON advisor_credentials;
CREATE POLICY advisor_credentials_isolation ON advisor_credentials
    FOR ALL
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

DROP POLICY IF EXISTS app_usage_events_isolation ON app_usage_events;
CREATE POLICY app_usage_events_isolation ON app_usage_events
    FOR ALL
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

DROP POLICY IF EXISTS agent_output_reviews_isolation ON agent_output_reviews;
CREATE POLICY agent_output_reviews_isolation ON agent_output_reviews
    FOR ALL
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

-- Authenticated users may read configs (not write — service role / migrations write)
DROP POLICY IF EXISTS configs_read_authenticated ON configs;
CREATE POLICY configs_read_authenticated ON configs
    FOR SELECT
    TO authenticated
    USING (true);

-- Firms: readable if advisor belongs to firm (v1 soft link)
DROP POLICY IF EXISTS firms_member_read ON firms;
CREATE POLICY firms_member_read ON firms
    FOR SELECT
    TO authenticated
    USING (
        id IN (SELECT firm_id FROM advisors WHERE id = auth.uid() AND firm_id IS NOT NULL)
    );

-- audit_logs: advisors can read their own rows; inserts via service role typically
DROP POLICY IF EXISTS audit_logs_advisor_read ON audit_logs;
CREATE POLICY audit_logs_advisor_read ON audit_logs
    FOR SELECT
    TO authenticated
    USING (advisor_id = auth.uid());

COMMENT ON TABLE advisor_credentials IS
    'Third-party API keys. Prefer vault.secrets; never return raw secrets via API.';
COMMENT ON TABLE agent_output_reviews IS
    'Accuracy audit: agent JSON vs advisor-saved JSON for HITL measurement.';
COMMENT ON TABLE staged_trades IS
    'Order blotter. External OMS is a simulator only.';
