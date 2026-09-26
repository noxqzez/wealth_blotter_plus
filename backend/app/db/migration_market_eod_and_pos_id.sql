-- Additive: EOD market history (equity + crypto) + document PosID on positions.
-- Apply after schema.sql / cutover. Safe to re-run (IF NOT EXISTS).

-- ---------------------------------------------------------------------------
-- Canonical end-of-day prices (charts + reusable history)
-- price_date = equity session calendar date. Crypto 24/7 prints are mapped onto
-- the same equity trading dates so series align for scenario graphs.
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

COMMENT ON TABLE market_eod_prices IS
    'EOD closes for equities and crypto. Crypto bars use equity session price_date for alignment.';
COMMENT ON COLUMN market_eod_prices.price_date IS
    'Equity calendar session date; crypto EOD mapped to this date (not 24/7 timestamps).';
COMMENT ON COLUMN market_eod_prices.close_price IS
    'End-of-day close only — no intraday ticks in this table.';

-- ---------------------------------------------------------------------------
-- Immutable copy of bars actually consumed by a scenario / agent run (audit)
-- ---------------------------------------------------------------------------
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

COMMENT ON TABLE scenario_market_price_snapshots IS
    'Frozen EOD bars used in a scenario/agent run for audit and troubleshooting.';

ALTER TABLE market_eod_prices ENABLE ROW LEVEL SECURITY;
ALTER TABLE scenario_market_price_snapshots ENABLE ROW LEVEL SECURITY;

-- Authenticated read of canonical EOD (shared market data); writes via service role
DROP POLICY IF EXISTS market_eod_prices_read_authenticated ON market_eod_prices;
CREATE POLICY market_eod_prices_read_authenticated ON market_eod_prices
    FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS scenario_mkt_snap_select_own ON scenario_market_price_snapshots;
CREATE POLICY scenario_mkt_snap_select_own ON scenario_market_price_snapshots
    FOR SELECT TO authenticated
    USING (advisor_id IS NULL OR advisor_id = auth.uid());

DROP POLICY IF EXISTS scenario_mkt_snap_insert_own ON scenario_market_price_snapshots;
CREATE POLICY scenario_mkt_snap_insert_own ON scenario_market_price_snapshots
    FOR INSERT TO authenticated
    WITH CHECK (advisor_id IS NULL OR advisor_id = auth.uid());

-- PosID: portfolio_positions.id is the stable unique position reference (PosID).
-- Application upserts must preserve id across UPDATE PORTFOLIO refreshes.
COMMENT ON COLUMN portfolio_positions.id IS
    'PosID — stable unique position reference for blotter, audit, and Excel.';
COMMENT ON TABLE portfolio_positions IS
    'MVP position blotter; id is PosID. Prices from Yahoo on UPDATE PORTFOLIO.';
