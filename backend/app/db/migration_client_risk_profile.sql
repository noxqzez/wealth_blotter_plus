-- Optional client suitability dial for future advisor checks (unused in MVP flows).
-- 1 = most conservative … 5 = most aggressive; default 3 (neutral).

ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS risk_profile SMALLINT NOT NULL DEFAULT 3;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'clients_risk_profile_check'
    ) THEN
        ALTER TABLE clients
            ADD CONSTRAINT clients_risk_profile_check
            CHECK (risk_profile BETWEEN 1 AND 5);
    END IF;
END $$;

COMMENT ON COLUMN clients.risk_profile IS
    'Suitability dial 1–5 for future advisor checks; not enforced in MVP.';
