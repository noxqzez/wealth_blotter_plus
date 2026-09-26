-- WF-01: securities list import — draft staging, per-position Ccy
-- Apply after schema.sql / cutover_mvp.sql
-- (Import accuracy diffs removed — use audit_events messages/warnings instead.)

-- ---------------------------------------------------------------------------
-- Portfolios: draft staging (PosIDs assigned only on base promotion)
-- ---------------------------------------------------------------------------
ALTER TABLE portfolios
    ADD COLUMN IF NOT EXISTS folio_stage TEXT NOT NULL DEFAULT 'BASE';

ALTER TABLE portfolios
    DROP CONSTRAINT IF EXISTS portfolios_folio_stage_check;
ALTER TABLE portfolios
    ADD CONSTRAINT portfolios_folio_stage_check
        CHECK (folio_stage IN ('DRAFT', 'BASE'));

ALTER TABLE portfolios
    ADD COLUMN IF NOT EXISTS import_draft_id UUID;

ALTER TABLE portfolios
    ADD COLUMN IF NOT EXISTS draft_folio JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN portfolios.folio_stage IS
    'DRAFT = validate-import staging; BASE = enriched blotter in portfolio_positions.';
COMMENT ON COLUMN portfolios.import_draft_id IS
    'Import session id correlating validate-import → build-base.';
COMMENT ON COLUMN portfolios.draft_folio IS
    'Normalized draft positions (no PosID) until build-base promotion.';

-- ---------------------------------------------------------------------------
-- Positions: advisor Ccy + price status
-- ---------------------------------------------------------------------------
ALTER TABLE portfolio_positions
    ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'USD';

ALTER TABLE portfolio_positions
    ADD COLUMN IF NOT EXISTS price_status TEXT NOT NULL DEFAULT 'VALID';

ALTER TABLE portfolio_positions
    DROP CONSTRAINT IF EXISTS portfolio_positions_price_status_check;
ALTER TABLE portfolio_positions
    ADD CONSTRAINT portfolio_positions_price_status_check
        CHECK (price_status IN ('VALID', 'MANUAL_PRICE_REQUIRED'));

COMMENT ON COLUMN portfolio_positions.currency IS
    'Position Ccy (USD|HKD). Ticker-derived currency wins on advisor mismatch.';
COMMENT ON COLUMN portfolio_positions.price_status IS
    'VALID when Yahoo spot present; MANUAL_PRICE_REQUIRED when spot unavailable.';
