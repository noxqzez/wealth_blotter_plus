-- WF-02: order blotter place-all + HITL KPI
-- Status Pending → Executed (dummy OMS). orders_hitl when Excel blotter ≠ staged.

ALTER TABLE staged_trades
    ADD COLUMN IF NOT EXISTS client_alias TEXT,
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'Pending',
    ADD COLUMN IF NOT EXISTS exec_type TEXT NOT NULL DEFAULT 'Market';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'staged_trades_status_check'
    ) THEN
        ALTER TABLE staged_trades
            ADD CONSTRAINT staged_trades_status_check
            CHECK (status IN ('Pending', 'Executed'));
    END IF;
END $$;

COMMENT ON COLUMN staged_trades.status IS
    'Pending until Place All Orders; Executed = dummy OMS fill.';
COMMENT ON COLUMN staged_trades.exec_type IS
    'Always Market for Phase-1 UAT.';
COMMENT ON COLUMN staged_trades.client_alias IS
    'Advisor client alias copied onto the order blotter for review.';

ALTER TABLE scenario_runs
    ADD COLUMN IF NOT EXISTS orders_hitl BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN scenario_runs.orders_hitl IS
    'True when Place All Orders saw Excel blotter edits vs staged (HITL KPI).';
