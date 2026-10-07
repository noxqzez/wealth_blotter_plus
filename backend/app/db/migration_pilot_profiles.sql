-- Phase 1 pilot: advisor profiles (approval gate), client ownership, telemetry attribution.
-- Apply on Supabase SQL editor (or migration runner). Safe additive where possible.

-- ---------------------------------------------------------------------------
-- profiles (Supabase Auth uid = id; no custom password table)
-- ---------------------------------------------------------------------------
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

CREATE INDEX IF NOT EXISTS idx_profiles_approval_status
    ON profiles (approval_status);
CREATE INDEX IF NOT EXISTS idx_profiles_email
    ON profiles (lower(email));

COMMENT ON TABLE profiles IS
    'Pilot advisor gate: Auth uid → approval_status. Passwords live in auth.users only.';

ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS profiles_select_own ON profiles;
CREATE POLICY profiles_select_own ON profiles
    FOR SELECT TO authenticated
    USING (id = auth.uid());

-- No insert/update policies for authenticated — service role (API / dashboard) only.

-- ---------------------------------------------------------------------------
-- clients.advisor_id (isolate pilot blotters)
-- ---------------------------------------------------------------------------
ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS advisor_id UUID;

UPDATE clients
SET advisor_id = '00000000-0000-4000-8000-000000000001'::uuid
WHERE advisor_id IS NULL;

ALTER TABLE clients
    ALTER COLUMN advisor_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_clients_advisor_id ON clients (advisor_id);

ALTER TABLE clients DROP CONSTRAINT IF EXISTS clients_client_alias_key;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'clients_advisor_alias_uidx'
    ) THEN
        ALTER TABLE clients
            ADD CONSTRAINT clients_advisor_alias_uidx
            UNIQUE (advisor_id, client_alias);
    END IF;
END $$;

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

-- ---------------------------------------------------------------------------
-- usage_metrics.advisor_id
-- ---------------------------------------------------------------------------
ALTER TABLE usage_metrics
    ADD COLUMN IF NOT EXISTS advisor_id UUID;

CREATE INDEX IF NOT EXISTS idx_usage_metrics_advisor_id
    ON usage_metrics (advisor_id)
    WHERE advisor_id IS NOT NULL;

DROP POLICY IF EXISTS usage_metrics_select_own ON usage_metrics;
CREATE POLICY usage_metrics_select_own ON usage_metrics
    FOR SELECT TO authenticated
    USING (advisor_id = auth.uid());

DROP POLICY IF EXISTS usage_metrics_insert_own ON usage_metrics;
CREATE POLICY usage_metrics_insert_own ON usage_metrics
    FOR INSERT TO authenticated
    WITH CHECK (advisor_id = auth.uid());
