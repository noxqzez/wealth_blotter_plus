-- Indexed base vs opti cumulative performance for PDF / Gamma charts.
ALTER TABLE scenario_runs
    ADD COLUMN IF NOT EXISTS performance_series JSONB;

COMMENT ON COLUMN scenario_runs.performance_series IS
    'Starting Positions vs Optimized Portfolio index series (start=100) over lookback.';
