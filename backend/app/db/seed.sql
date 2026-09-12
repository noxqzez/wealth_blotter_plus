-- Seed runtime configs from repository defaults.
-- Re-run safely via upsert on primary key `key`.

INSERT INTO configs (key, config, version, updated_at)
VALUES (
    'technical_limits',
    '{
      "max_tickers_per_request": 50,
      "api_rate_limit_rpm": 120,
      "execution_timeout_seconds": 120,
      "max_batch_ingest_rows": 500,
      "max_self_heal_retries": 3
    }'::jsonb,
    1,
    now()
)
ON CONFLICT (key) DO UPDATE
SET
    config = EXCLUDED.config,
    version = configs.version + 1,
    updated_at = now();

INSERT INTO configs (key, config, version, updated_at)
VALUES (
    'business_guardrails',
    '{
      "min_cash_buffer_pct": 0.05,
      "max_single_name_weight_pct": 0.10,
      "max_crypto_weight_pct": 0.05,
      "confidence_threshold": 0.85,
      "jurisdictions": {
        "sfc_enabled": true,
        "sec_enabled": true,
        "allowed_markets": ["us", "hk", "crypto"]
      }
    }'::jsonb,
    1,
    now()
)
ON CONFLICT (key) DO UPDATE
SET
    config = EXCLUDED.config,
    version = configs.version + 1,
    updated_at = now();
