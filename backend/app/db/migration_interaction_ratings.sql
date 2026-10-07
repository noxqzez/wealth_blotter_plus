-- Interaction ratings: optional 1–5 star feedback after opti_folio (one per simulation).

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

ALTER TABLE interaction_ratings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS interaction_ratings_select_own ON interaction_ratings;
CREATE POLICY interaction_ratings_select_own ON interaction_ratings
    FOR SELECT TO authenticated
    USING (advisor_id = auth.uid());

DROP POLICY IF EXISTS interaction_ratings_insert_own ON interaction_ratings;
CREATE POLICY interaction_ratings_insert_own ON interaction_ratings
    FOR INSERT TO authenticated
    WITH CHECK (advisor_id = auth.uid());

COMMENT ON TABLE interaction_ratings IS
    'Optional advisor star rating (1–5) after opti_folio; one row per scenario_run.';
COMMENT ON COLUMN interaction_ratings.rating IS
    'User satisfaction stars 1–5 for this simulation.';
