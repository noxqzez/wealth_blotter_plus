-- Canonical trading_rules seeds (one row per policy — no duplicates).
-- Categories: PORTFOLIO | TRADE | CLIENT
-- Weight / universe / jurisdiction owned HERE — not in business_guardrails.
-- Technical ingest caps live in configs.technical_limits only (not here).
--
-- Already hard-coded in the cvxpy optimizer (no trading_rules row required):
--   long-only (weight >= 0), fully invested (sum weights == 1.0), zeros allowed

-- PORTFOLIO — max single name (keep max_value == parameters.max_single_weight)
INSERT INTO trading_rules (rule_name, rule_category, min_value, max_value, parameters, is_active)
VALUES (
    'max_single_name_weight',
    'PORTFOLIO',
    NULL,
    0.25,
    '{"max_single_weight": 0.25, "unit": "weight_pct", "reason": "Max weight per name 25%"}'::jsonb,
    true
)
ON CONFLICT (rule_name) DO UPDATE
SET rule_category = EXCLUDED.rule_category,
    parameters = EXCLUDED.parameters,
    max_value = EXCLUDED.max_value,
    is_active = true,
    updated_at = now();

-- PORTFOLIO — minimum cash buffer (keep min_value == parameters.min_cash_weight)
INSERT INTO trading_rules (rule_name, rule_category, min_value, max_value, parameters, is_active)
VALUES (
    'min_cash_buffer',
    'PORTFOLIO',
    0.05,
    NULL,
    '{"min_cash_weight": 0.05, "reason": "Minimum cash buffer 5%"}'::jsonb,
    true
)
ON CONFLICT (rule_name) DO UPDATE
SET rule_category = EXCLUDED.rule_category,
    parameters = EXCLUDED.parameters,
    min_value = EXCLUDED.min_value,
    is_active = true,
    updated_at = now();

-- PORTFOLIO — approved crypto investable universe (set coins HERE — not in configs)
INSERT INTO trading_rules (rule_name, rule_category, min_value, max_value, parameters, is_active)
VALUES (
    'approved_crypto_universe',
    'PORTFOLIO',
    NULL,
    NULL,
    '{"approved_tickers": ["BTC", "ETH", "DOGE", "XRP", "SOL"], "reason": "Approved crypto investable universe"}'::jsonb,
    true
)
ON CONFLICT (rule_name) DO UPDATE
SET rule_category = EXCLUDED.rule_category,
    parameters = EXCLUDED.parameters,
    is_active = true,
    updated_at = now();

-- CLIENT — firm jurisdictions / allowed markets
INSERT INTO trading_rules (rule_name, rule_category, min_value, max_value, parameters, is_active)
VALUES (
    'jurisdictions',
    'CLIENT',
    NULL,
    NULL,
    '{"sfc_enabled": true, "sec_enabled": true, "allowed_markets": ["us", "hk", "crypto"], "reason": "Firm allowed markets"}'::jsonb,
    true
)
ON CONFLICT (rule_name) DO UPDATE
SET rule_category = EXCLUDED.rule_category,
    parameters = EXCLUDED.parameters,
    is_active = true,
    updated_at = now();

-- CLIENT — Client 2 cannot hold approved coins
INSERT INTO trading_rules (rule_name, rule_category, min_value, max_value, parameters, is_active)
VALUES (
    'client_2_no_coins',
    'CLIENT',
    NULL,
    NULL,
    '{
      "client_alias": "Client 2",
      "ineligible_tickers": ["BTC-USD", "ETH-USD", "DOGE-USD", "XRP-USD", "SOL-USD"],
      "reason": "Client 2 cannot hold any coins"
    }'::jsonb,
    true
)
ON CONFLICT (rule_name) DO UPDATE
SET rule_category = EXCLUDED.rule_category,
    parameters = EXCLUDED.parameters,
    is_active = true,
    updated_at = now();

-- PORTFOLIO — asset-class sleeve weight (BLANK TEMPLATE — fill + activate to enforce)
INSERT INTO trading_rules (rule_name, rule_category, min_value, max_value, parameters, is_active)
VALUES (
    'asset_class_weight_cap',
    'PORTFOLIO',
    NULL,
    NULL,
    '{
      "asset_class": null,
      "max_total_weight": null,
      "reason": "Template: set asset_class (e.g. CRYPTO) and max_total_weight fraction"
    }'::jsonb,
    false
)
ON CONFLICT (rule_name) DO UPDATE
SET rule_category = EXCLUDED.rule_category,
    parameters = EXCLUDED.parameters,
    is_active = EXCLUDED.is_active,
    updated_at = now();

-- Remove legacy duplicates / technical mirrors
DELETE FROM trading_rules WHERE rule_name IN (
    'max_single_position_weight_25pct',
    'max_batch_ingest_rows'
);
