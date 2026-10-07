-- WF-02 Market Scenarios (aligns with schema.sql).
-- Apply in Supabase SQL Editor after WF-01 migrations.
-- Weight / position caps live in trading_rules (PORTFOLIO); see seed_trading_rules_examples.sql.

COMMENT ON TABLE trading_rules IS
    'Firm-wide R&C. Categories: PORTFOLIO | TRADE | CLIENT | TECHNICAL + parameters jsonb. '
    'Legacy aliases POSITION_LIMIT/CONCENTRATION→PORTFOLIO, TRADING_RESTRICTION→TRADE, '
    'CLIENT_RESTRICTION→CLIENT still accepted by the optimizer.';

COMMENT ON COLUMN trading_rules.rule_category IS
    'PORTFOLIO | TRADE | CLIENT | TECHNICAL (legacy aliases mapped in code).';

COMMENT ON COLUMN trading_rules.parameters IS
    'PORTFOLIO: {max_single_weight|min_cash_weight|approved_tickers|asset_class+max_total_weight}; '
    'TRADE: {restricted_tickers[]}; '
    'CLIENT: {client_id|client_alias, ineligible_tickers[]} | jurisdictions {allowed_markets,sfc_enabled,sec_enabled}; '
    'TECHNICAL: {maps_to_config, ...}';

-- ---------------------------------------------------------------------------
-- scenario_runs
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

COMMENT ON TABLE scenario_runs IS
    'WF-02 scenario lifecycle: parse → optional param edit → run → PDF artifacts.';

-- ---------------------------------------------------------------------------
-- scenario_param_diffs
-- ---------------------------------------------------------------------------
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

COMMENT ON TABLE scenario_param_diffs IS
    'Deterministic field diffs: agent parse vs human Update parameters (accuracy KPI).';

-- ---------------------------------------------------------------------------
-- staged_trades (restored with order_id)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staged_trades (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id            TEXT NOT NULL UNIQUE,
    scenario_run_id     UUID NOT NULL REFERENCES scenario_runs (id) ON DELETE CASCADE,
    advisor_id          UUID NOT NULL,
    portfolio_id        UUID NOT NULL REFERENCES portfolios (id) ON DELETE CASCADE,
    ticker              TEXT NOT NULL,
    asset_class         TEXT NOT NULL,
    side                TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity            NUMERIC(18, 6) NOT NULL,
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

COMMENT ON TABLE staged_trades IS
    'WF-02 proposed trades base→opti; order_id is the citeable unique order id.';
COMMENT ON COLUMN staged_trades.order_id IS
    'Unique order identifier for user/system/compliance citation (not PosID).';

-- Optional FK from EOD snapshots → scenario_runs (NOT VALID: legacy rows allowed)
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

-- ---------------------------------------------------------------------------
-- RLS
-- ---------------------------------------------------------------------------
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
