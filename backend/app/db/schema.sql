-- MVP schema: portfolio update via Excel (client_alias | ticker | qty | asset_class)
-- Auth/SSO, agents, HITL come later. Dev uses service-role writes + optional bypass.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";

-- ---------------------------------------------------------------------------
-- Clients (keyed by client_alias for MVP paste flow)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clients (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_alias    TEXT NOT NULL,
    advisor_id      UUID NOT NULL DEFAULT '00000000-0000-4000-8000-000000000001'::uuid,
    -- Optional suitability dial for future advisor checks (1=cold … 5=aggressive). Unused in MVP flows.
    risk_profile    SMALLINT NOT NULL DEFAULT 3
                    CHECK (risk_profile BETWEEN 1 AND 5),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (advisor_id, client_alias)
);

-- ---------------------------------------------------------------------------
-- One portfolio per client (MVP)
-- folio_stage / draft_folio / import_draft_id: WF-01 import staging
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portfolios (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       UUID NOT NULL UNIQUE REFERENCES clients (id) ON DELETE CASCADE,
    cash_balance    NUMERIC(15, 2) NOT NULL DEFAULT 0,
    currency        TEXT NOT NULL DEFAULT 'USD',
    folio_stage     TEXT NOT NULL DEFAULT 'BASE'
                    CHECK (folio_stage IN ('DRAFT', 'BASE')),
    import_draft_id UUID,
    draft_folio     JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- True when this import needed advisor HITL (Reprocess / Drop Invalid).
    import_hitl     BOOLEAN NOT NULL DEFAULT false,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_portfolios_client_id ON portfolios (client_id);
CREATE INDEX IF NOT EXISTS idx_clients_advisor_id ON clients (advisor_id);

-- ---------------------------------------------------------------------------
-- Positions blotter
-- id = PosID (stable unique position reference for audit / Excel / troubleshooting)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portfolio_positions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id         UUID NOT NULL REFERENCES portfolios (id) ON DELETE CASCADE,
    ticker               TEXT NOT NULL,
    asset_class          TEXT NOT NULL,
    quantity             NUMERIC(18, 6) NOT NULL,
    spot_price           NUMERIC(15, 6) NOT NULL DEFAULT 0,
    short_name           TEXT,
    notional_value       NUMERIC(15, 2) NOT NULL DEFAULT 0,
    current_weight_pct   NUMERIC(8, 6),
    currency             TEXT NOT NULL DEFAULT 'USD',
    price_status         TEXT NOT NULL DEFAULT 'VALID'
                         CHECK (price_status IN ('VALID', 'MANUAL_PRICE_REQUIRED')),
    valuation_timestamp  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (portfolio_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_portfolio_positions_portfolio_id
    ON portfolio_positions (portfolio_id);

COMMENT ON COLUMN portfolio_positions.id IS
    'PosID — stable unique position reference for blotter, audit, and Excel.';
COMMENT ON COLUMN portfolio_positions.currency IS
    'Position Ccy (USD|HKD). Assigned at base promotion.';
COMMENT ON COLUMN portfolio_positions.price_status IS
    'VALID when Yahoo spot present; MANUAL_PRICE_REQUIRED when spot unavailable.';

-- ---------------------------------------------------------------------------
-- EOD market history (equity + crypto) — charts, scenarios, audit
-- See also migration_market_eod_and_pos_id.sql
-- price_date uses equity session calendar; crypto closes mapped to same dates.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_eod_prices (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker           TEXT NOT NULL,
    asset_class      TEXT NOT NULL,
    price_date       DATE NOT NULL,
    close_price      NUMERIC(18, 8) NOT NULL,
    currency         TEXT NOT NULL DEFAULT 'USD',
    source_name      TEXT NOT NULL,
    provider_symbol  TEXT,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (ticker, price_date, source_name)
);

CREATE INDEX IF NOT EXISTS idx_market_eod_prices_ticker_date
    ON market_eod_prices (ticker, price_date DESC);
CREATE INDEX IF NOT EXISTS idx_market_eod_prices_asset_class_date
    ON market_eod_prices (asset_class, price_date DESC);
CREATE INDEX IF NOT EXISTS idx_market_eod_prices_source
    ON market_eod_prices (source_name);

CREATE TABLE IF NOT EXISTS scenario_market_price_snapshots (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scenario_run_id      UUID NOT NULL,
    advisor_id           UUID,
    ticker               TEXT NOT NULL,
    asset_class          TEXT NOT NULL,
    price_date           DATE NOT NULL,
    close_price          NUMERIC(18, 8) NOT NULL,
    currency             TEXT NOT NULL DEFAULT 'USD',
    source_name          TEXT NOT NULL,
    market_eod_price_id  UUID REFERENCES market_eod_prices (id) ON DELETE SET NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scenario_mkt_snap_run
    ON scenario_market_price_snapshots (scenario_run_id);
CREATE INDEX IF NOT EXISTS idx_scenario_mkt_snap_ticker_date
    ON scenario_market_price_snapshots (ticker, price_date);
CREATE INDEX IF NOT EXISTS idx_scenario_mkt_snap_advisor
    ON scenario_market_price_snapshots (advisor_id);

-- ---------------------------------------------------------------------------
-- Business rules (table for later agent lookup / audit — unused in MVP flow)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trading_rules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_name       TEXT NOT NULL UNIQUE,
    rule_category   TEXT NOT NULL,
    min_value       NUMERIC(10, 4),
    max_value       NUMERIC(10, 4),
    parameters      JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Technical rules-as-code mirror (editable in repo seeds; cached by API)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS configs (
    key         VARCHAR(128) PRIMARY KEY,
    config      JSONB NOT NULL,
    version     INT NOT NULL DEFAULT 1,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  UUID
);

-- ---------------------------------------------------------------------------
-- Usage / telemetry (PDF downloads etc. — later; table ready)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage_metrics (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_action            TEXT NOT NULL,
    endpoint_or_component   TEXT NOT NULL,
    client_alias            TEXT,
    advisor_id              UUID,
    execution_time_ms       INTEGER,
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_usage_metrics_created_at ON usage_metrics (created_at);
CREATE INDEX IF NOT EXISTS idx_usage_metrics_event_action ON usage_metrics (event_action);
CREATE INDEX IF NOT EXISTS idx_usage_metrics_advisor_id
    ON usage_metrics (advisor_id)
    WHERE advisor_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Compliance Phase 1: unified audit corpus + chat persistence
-- (also shipped as migration_compliance_phase1.sql for additive deploys)
--
-- usage_metrics vs audit_events:
--   usage_metrics = lightweight product telemetry (separate).
--   audit_events  = compliance investigation corpus (application_event |
--                   agent_telemetry). Portfolio actions emit here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type         TEXT NOT NULL
                        CHECK (source_type IN ('application_event', 'agent_telemetry')),
    source_name         TEXT NOT NULL,
    event_action        TEXT NOT NULL,
    event_type          TEXT,
    -- Nullable for MVP service-role writers until Auth/SSO; RLS scopes on it.
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

-- RLS enabled for future Auth; MVP writes use service role
ALTER TABLE clients ENABLE ROW LEVEL SECURITY;
ALTER TABLE portfolios ENABLE ROW LEVEL SECURITY;
ALTER TABLE portfolio_positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE trading_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE interaction_ratings ENABLE ROW LEVEL SECURITY;
ALTER TABLE market_eod_prices ENABLE ROW LEVEL SECURITY;
ALTER TABLE scenario_market_price_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE compliance_chat_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE compliance_chat_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE compliance_chat_traces ENABLE ROW LEVEL SECURITY;

-- Phase 1 pilot profiles (Auth uid = id)
CREATE TABLE IF NOT EXISTS profiles (
    id                  UUID PRIMARY KEY,
    email               TEXT NOT NULL,
    role                TEXT NOT NULL DEFAULT 'advisor'
                        CHECK (role = 'advisor'),
    approval_status     TEXT NOT NULL DEFAULT 'PENDING_APPROVAL'
                        CHECK (approval_status IN (
                            'PENDING_APPROVAL',
                            'APPROVED',
                            'REJECTED'
                        )),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS profiles_select_own ON profiles;
CREATE POLICY profiles_select_own ON profiles
    FOR SELECT TO authenticated
    USING (id = auth.uid());

DROP POLICY IF EXISTS clients_select_own ON clients;
CREATE POLICY clients_select_own ON clients
    FOR SELECT TO authenticated
    USING (advisor_id = auth.uid());
DROP POLICY IF EXISTS clients_insert_own ON clients;
CREATE POLICY clients_insert_own ON clients
    FOR INSERT TO authenticated
    WITH CHECK (advisor_id = auth.uid());
DROP POLICY IF EXISTS clients_update_own ON clients;
CREATE POLICY clients_update_own ON clients
    FOR UPDATE TO authenticated
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

DROP POLICY IF EXISTS portfolios_select_own ON portfolios;
CREATE POLICY portfolios_select_own ON portfolios
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM clients c
            WHERE c.id = client_id AND c.advisor_id = auth.uid()
        )
    );
DROP POLICY IF EXISTS portfolios_write_own ON portfolios;
CREATE POLICY portfolios_write_own ON portfolios
    FOR ALL TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM clients c
            WHERE c.id = client_id AND c.advisor_id = auth.uid()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM clients c
            WHERE c.id = client_id AND c.advisor_id = auth.uid()
        )
    );

DROP POLICY IF EXISTS portfolio_positions_select_own ON portfolio_positions;
CREATE POLICY portfolio_positions_select_own ON portfolio_positions
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1
            FROM portfolios p
            JOIN clients c ON c.id = p.client_id
            WHERE p.id = portfolio_id AND c.advisor_id = auth.uid()
        )
    );
DROP POLICY IF EXISTS portfolio_positions_write_own ON portfolio_positions;
CREATE POLICY portfolio_positions_write_own ON portfolio_positions
    FOR ALL TO authenticated
    USING (
        EXISTS (
            SELECT 1
            FROM portfolios p
            JOIN clients c ON c.id = p.client_id
            WHERE p.id = portfolio_id AND c.advisor_id = auth.uid()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1
            FROM portfolios p
            JOIN clients c ON c.id = p.client_id
            WHERE p.id = portfolio_id AND c.advisor_id = auth.uid()
        )
    );

DROP POLICY IF EXISTS usage_metrics_select_own ON usage_metrics;
CREATE POLICY usage_metrics_select_own ON usage_metrics
    FOR SELECT TO authenticated
    USING (advisor_id = auth.uid());
DROP POLICY IF EXISTS usage_metrics_insert_own ON usage_metrics;
CREATE POLICY usage_metrics_insert_own ON usage_metrics
    FOR INSERT TO authenticated
    WITH CHECK (advisor_id = auth.uid());

-- Authenticated read of configs / active trading rules (future SSO)
DROP POLICY IF EXISTS configs_read_authenticated ON configs;
CREATE POLICY configs_read_authenticated ON configs
    FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS trading_rules_read_authenticated ON trading_rules;
CREATE POLICY trading_rules_read_authenticated ON trading_rules
    FOR SELECT TO authenticated USING (is_active = true);

DROP POLICY IF EXISTS market_eod_prices_read_authenticated ON market_eod_prices;
CREATE POLICY market_eod_prices_read_authenticated ON market_eod_prices
    FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS interaction_ratings_select_own ON interaction_ratings;
CREATE POLICY interaction_ratings_select_own ON interaction_ratings
    FOR SELECT TO authenticated
    USING (advisor_id = auth.uid());

DROP POLICY IF EXISTS interaction_ratings_insert_own ON interaction_ratings;
CREATE POLICY interaction_ratings_insert_own ON interaction_ratings
    FOR INSERT TO authenticated
    WITH CHECK (advisor_id = auth.uid());

DROP POLICY IF EXISTS scenario_mkt_snap_select_own ON scenario_market_price_snapshots;
CREATE POLICY scenario_mkt_snap_select_own ON scenario_market_price_snapshots
    FOR SELECT TO authenticated
    USING (advisor_id IS NULL OR advisor_id = auth.uid());

DROP POLICY IF EXISTS scenario_mkt_snap_insert_own ON scenario_market_price_snapshots;
CREATE POLICY scenario_mkt_snap_insert_own ON scenario_market_price_snapshots
    FOR INSERT TO authenticated
    WITH CHECK (advisor_id IS NULL OR advisor_id = auth.uid());

DROP POLICY IF EXISTS audit_events_select_own ON audit_events;
CREATE POLICY audit_events_select_own ON audit_events
    FOR SELECT TO authenticated
    USING (advisor_id = auth.uid());

DROP POLICY IF EXISTS audit_events_insert_own ON audit_events;
CREATE POLICY audit_events_insert_own ON audit_events
    FOR INSERT TO authenticated
    WITH CHECK (advisor_id = auth.uid());

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

COMMENT ON TABLE portfolio_positions IS
    'MVP position blotter; id is PosID. Prices from Yahoo on UPDATE PORTFOLIO.';
COMMENT ON TABLE trading_rules IS
    'Firm-wide R&C. Categories: PORTFOLIO | TRADE | CLIENT | TECHNICAL + parameters jsonb.';
COMMENT ON TABLE configs IS 'Technical limits JSON (rules-as-code seed)';
COMMENT ON TABLE market_eod_prices IS
    'EOD closes for equities and crypto. Crypto bars use equity session price_date for alignment.';
COMMENT ON TABLE scenario_market_price_snapshots IS
    'Frozen EOD bars used in a scenario/agent run for audit and troubleshooting.';
COMMENT ON TABLE audit_events IS
    'Immutable unified audit corpus. source_type: application_event | agent_telemetry.';
COMMENT ON TABLE usage_metrics IS
    'Lightweight product telemetry; separate from compliance audit_events corpus.';
COMMENT ON TABLE compliance_chat_traces IS
    'Per assistant-turn trace with citations into audit_events + trading_rules/configs.';
COMMENT ON TABLE scenario_runs IS
    'WF-02 scenario lifecycle: parse → optional param edit → run → PDF artifacts.';
COMMENT ON TABLE scenario_param_diffs IS
    'Deterministic field diffs: agent parse vs human Update parameters (accuracy KPI).';
COMMENT ON TABLE staged_trades IS
    'WF-02 proposed trades base→opti; order_id is the citeable unique order id.';

-- ---------------------------------------------------------------------------
-- WF-02 Scenario Running (see migration_scenario_wf02.sql)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scenario_runs (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    advisor_id              UUID NOT NULL,
    portfolio_id            UUID NOT NULL REFERENCES portfolios (id) ON DELETE CASCADE,
    client_id               UUID REFERENCES clients (id) ON DELETE SET NULL,
    status                  TEXT NOT NULL DEFAULT 'PARSED'
                            CHECK (status IN (
                                'PARSED',
                                'PARAMS_UPDATED',
                                'RUNNING',
                                'COMPLETED',
                                'FAILED'
                            )),
    phase                   TEXT NOT NULL DEFAULT 'phase1'
                            CHECK (phase IN ('phase1', 'phase2')),
    user_prompt             TEXT NOT NULL,
    agent_scenario_params   JSONB NOT NULL DEFAULT '{}'::jsonb,
    scenario_params         JSONB NOT NULL DEFAULT '{}'::jsonb,
    scenario_params_version INT NOT NULL DEFAULT 1,
    strategy_view           JSONB,
    risk_metrics_base       JSONB,
    risk_metrics_opti       JSONB,
    opti_folio              JSONB NOT NULL DEFAULT '[]'::jsonb,
    talking_points          JSONB,
    performance_series      JSONB,
    feature_flags           JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- True when Place All Orders saw Excel edits vs system-staged trades.
    orders_hitl             BOOLEAN NOT NULL DEFAULT false,
    error_code              TEXT,
    error_message           TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scenario_runs_advisor
    ON scenario_runs (advisor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_scenario_runs_portfolio
    ON scenario_runs (portfolio_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_scenario_runs_status
    ON scenario_runs (status);

CREATE TABLE IF NOT EXISTS scenario_param_diffs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scenario_run_id     UUID NOT NULL REFERENCES scenario_runs (id) ON DELETE CASCADE,
    advisor_id          UUID NOT NULL,
    field_path          TEXT NOT NULL,
    agent_value         JSONB,
    human_value         JSONB,
    is_exact_match      BOOLEAN NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scenario_param_diffs_run
    ON scenario_param_diffs (scenario_run_id);

CREATE TABLE IF NOT EXISTS staged_trades (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id            TEXT NOT NULL UNIQUE,
    scenario_run_id     UUID NOT NULL REFERENCES scenario_runs (id) ON DELETE CASCADE,
    advisor_id          UUID NOT NULL,
    portfolio_id        UUID NOT NULL REFERENCES portfolios (id) ON DELETE CASCADE,
    client_alias        TEXT,
    status              TEXT NOT NULL DEFAULT 'Pending'
                        CHECK (status IN ('Pending', 'Executed')),
    ticker              TEXT NOT NULL,
    asset_class         TEXT NOT NULL,
    side                TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity            NUMERIC(18, 6) NOT NULL,
    exec_type           TEXT NOT NULL DEFAULT 'Market',
    notional_value      NUMERIC(15, 2),
    from_weight_pct     NUMERIC(8, 6),
    to_weight_pct       NUMERIC(8, 6),
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_staged_trades_run
    ON staged_trades (scenario_run_id);
CREATE INDEX IF NOT EXISTS idx_staged_trades_portfolio
    ON staged_trades (portfolio_id);

-- ---------------------------------------------------------------------------
-- Interaction ratings (optional 1–5 stars after opti_folio; one per simulation)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS interaction_ratings (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    advisor_id          UUID NOT NULL,
    scenario_run_id     UUID NOT NULL REFERENCES scenario_runs (id) ON DELETE CASCADE,
    portfolio_id        UUID REFERENCES portfolios (id) ON DELETE SET NULL,
    session_id          TEXT,
    rating              SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    UNIQUE (scenario_run_id)
);

CREATE INDEX IF NOT EXISTS idx_interaction_ratings_advisor
    ON interaction_ratings (advisor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_interaction_ratings_created_at
    ON interaction_ratings (created_at DESC);

COMMENT ON TABLE interaction_ratings IS
    'Optional advisor star rating (1–5) after opti_folio; one row per scenario_run.';

-- Optional FK from EOD snapshots → scenario_runs (table created earlier in this file)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'scenario_mkt_snap_run_fk'
    ) THEN
        ALTER TABLE scenario_market_price_snapshots
            ADD CONSTRAINT scenario_mkt_snap_run_fk
            FOREIGN KEY (scenario_run_id) REFERENCES scenario_runs (id)
            ON DELETE CASCADE
            NOT VALID;
    END IF;
END $$;

ALTER TABLE scenario_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE scenario_param_diffs ENABLE ROW LEVEL SECURITY;
ALTER TABLE staged_trades ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS scenario_runs_all_own ON scenario_runs;
CREATE POLICY scenario_runs_all_own ON scenario_runs
    FOR ALL TO authenticated
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

DROP POLICY IF EXISTS scenario_param_diffs_all_own ON scenario_param_diffs;
CREATE POLICY scenario_param_diffs_all_own ON scenario_param_diffs
    FOR ALL TO authenticated
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

DROP POLICY IF EXISTS staged_trades_all_own ON staged_trades;
CREATE POLICY staged_trades_all_own ON staged_trades
    FOR ALL TO authenticated
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

-- ---------------------------------------------------------------------------
-- RAG POC: research PDFs → classify → embed (see migration_rag_poc.sql)
-- Embedding default: sentence-transformers/all-MiniLM-L6-v2 → vector(384)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_ingest_jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    advisor_id          UUID NOT NULL,
    storage_bucket      TEXT NOT NULL,
    storage_path        TEXT NOT NULL,
    original_filename   TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN (
                            'queued',
                            'processing',
                            'ready',
                            'quarantined',
                            'failed'
                        )),
    error_message       TEXT,
    document_id         UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    UNIQUE (storage_bucket, storage_path)
);

CREATE INDEX IF NOT EXISTS idx_document_ingest_jobs_status
    ON document_ingest_jobs (status, created_at);
CREATE INDEX IF NOT EXISTS idx_document_ingest_jobs_advisor_id
    ON document_ingest_jobs (advisor_id);

CREATE TABLE IF NOT EXISTS document_registry (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    advisor_id                  UUID NOT NULL,
    ingest_job_id               UUID REFERENCES document_ingest_jobs (id) ON DELETE SET NULL,
    storage_bucket              TEXT NOT NULL,
    storage_path                TEXT NOT NULL,
    original_filename           TEXT NOT NULL,
    asset_class                 TEXT NOT NULL
                                CHECK (asset_class IN (
                                    'US_Stock',
                                    'HK_Stock',
                                    'Crypto',
                                    'Neither'
                                )),
    ticker                      TEXT,
    source_firm                 TEXT,
    overall_sentiment           TEXT
                                CHECK (overall_sentiment IS NULL OR overall_sentiment IN (
                                    'Bullish',
                                    'Bearish',
                                    'Neutral'
                                )),
    classification_confidence   DOUBLE PRECISION
                                CHECK (
                                    classification_confidence IS NULL
                                    OR (
                                        classification_confidence >= 0.0
                                        AND classification_confidence <= 1.0
                                    )
                                ),
    status                      TEXT NOT NULL DEFAULT 'indexed'
                                CHECK (status IN ('indexed', 'quarantined')),
    embedding_model             TEXT,
    processed_at                TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (storage_bucket, storage_path)
);

CREATE INDEX IF NOT EXISTS idx_document_registry_advisor_id
    ON document_registry (advisor_id);
CREATE INDEX IF NOT EXISTS idx_document_registry_ticker
    ON document_registry (ticker)
    WHERE ticker IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_document_registry_asset_class
    ON document_registry (asset_class);

ALTER TABLE document_ingest_jobs
    DROP CONSTRAINT IF EXISTS document_ingest_jobs_document_id_fkey;
ALTER TABLE document_ingest_jobs
    ADD CONSTRAINT document_ingest_jobs_document_id_fkey
    FOREIGN KEY (document_id) REFERENCES document_registry (id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS document_chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES document_registry (id) ON DELETE CASCADE,
    advisor_id      UUID NOT NULL,
    chunk_index     INT NOT NULL,
    page_number     INT,
    content         TEXT NOT NULL,
    embedding       vector(384) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id
    ON document_chunks (document_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_advisor_id
    ON document_chunks (advisor_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding_hnsw
    ON document_chunks
    USING hnsw (embedding vector_cosine_ops);

CREATE OR REPLACE FUNCTION match_document_chunks(
    query_embedding vector(384),
    match_count int DEFAULT 5,
    filter_advisor_id uuid DEFAULT NULL,
    filter_ticker text DEFAULT NULL,
    filter_asset_class text DEFAULT NULL
)
RETURNS TABLE (
    chunk_id uuid,
    document_id uuid,
    content text,
    chunk_index int,
    page_number int,
    ticker text,
    asset_class text,
    storage_path text,
    original_filename text,
    similarity float
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        c.id AS chunk_id,
        c.document_id,
        c.content,
        c.chunk_index,
        c.page_number,
        r.ticker,
        r.asset_class,
        r.storage_path,
        r.original_filename,
        (1.0 - (c.embedding <=> query_embedding))::float AS similarity
    FROM document_chunks c
    INNER JOIN document_registry r ON r.id = c.document_id
    WHERE r.status = 'indexed'
      AND r.asset_class <> 'Neither'
      AND (filter_advisor_id IS NULL OR c.advisor_id = filter_advisor_id)
      AND (filter_ticker IS NULL OR r.ticker = filter_ticker)
      AND (filter_asset_class IS NULL OR r.asset_class = filter_asset_class)
    ORDER BY c.embedding <=> query_embedding
    LIMIT GREATEST(match_count, 1);
$$;

ALTER TABLE document_ingest_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS document_ingest_jobs_all_own ON document_ingest_jobs;
CREATE POLICY document_ingest_jobs_all_own ON document_ingest_jobs
    FOR ALL TO authenticated
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

DROP POLICY IF EXISTS document_registry_all_own ON document_registry;
CREATE POLICY document_registry_all_own ON document_registry
    FOR ALL TO authenticated
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

DROP POLICY IF EXISTS document_chunks_all_own ON document_chunks;
CREATE POLICY document_chunks_all_own ON document_chunks
    FOR ALL TO authenticated
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

COMMENT ON TABLE document_ingest_jobs IS
    'RAG POC queue: PDF upload → classify → embed.';
COMMENT ON TABLE document_registry IS
    'RAG POC document metadata from classification agent.';
COMMENT ON TABLE document_chunks IS
    'RAG POC text chunks + pgvector embeddings (384d MiniLM by default).';
