-- WF-01: HITL flag for portfolio import quality KPI.
-- True when advisor used Reprocess Corrected List or Drop Invalid and Proceed
-- before a successful validate-import for this portfolio's latest import cycle.

ALTER TABLE portfolios
    ADD COLUMN IF NOT EXISTS import_hitl BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN portfolios.import_hitl IS
    'True when latest import needed advisor HITL (reprocess / drop invalid). '
    'KPI: count(import_hitl) / count(portfolios) for garbage→folio quality.';
