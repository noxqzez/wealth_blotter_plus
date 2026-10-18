-- Seed technical limits (rules-as-code). Edit backend/app/db/seed_configs/ too.

INSERT INTO configs (key, config, version, updated_at)
VALUES (
    'technical_limits',
    '{
      "max_tickers_per_request": 50,
      "api_rate_limit_rpm": 120,
      "execution_timeout_seconds": 120,
      "max_batch_ingest_rows": 500,
      "max_self_heal_retries": 3,
      "market_data": {
        "yahoo_outbound_rpm": 30,
        "yahoo_burst_calls": 10,
        "yahoo_burst_window_seconds": 10,
        "yahoo_max_concurrency": 2,
        "yahoo_min_spacing_ms": 250,
        "yahoo_cache_ttl_seconds": 120,
        "yahoo_max_calls_per_agent_run": 20,
        "yahoo_max_retries": 3,
        "crypto_history_outbound_rpm": 20,
        "crypto_history_burst_calls": 5,
        "crypto_history_burst_window_seconds": 10,
        "crypto_history_max_concurrency": 1,
        "crypto_history_min_spacing_ms": 500,
        "crypto_history_cache_ttl_seconds": 300,
        "crypto_history_max_symbols_per_request": 10,
        "crypto_history_max_lookback_days": 1825,
        "crypto_history_max_calls_per_agent_run": 10,
        "crypto_history_max_retries": 3,
        "news_outbound_rpm": 10,
        "news_burst_calls": 3,
        "news_burst_window_seconds": 10,
        "news_max_concurrency": 1,
        "news_min_spacing_ms": 1000,
        "news_cache_ttl_seconds": 600,
        "news_max_articles_per_query": 10,
        "news_max_calls_per_agent_run": 5,
        "news_max_retries": 3
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

INSERT INTO configs (key, config, version, updated_at)
VALUES (
    'business_guardrails',
    '{
      "confidence_threshold": 0.85,
      "coin_temperature": 2
    }'::jsonb,
    1,
    now()
)
ON CONFLICT (key) DO UPDATE
SET
    config = EXCLUDED.config,
    version = configs.version + 1,
    updated_at = now();

-- WF-02 scenario technical defaults (lookback, timeouts, phase flags)
INSERT INTO configs (key, config, version, updated_at)
VALUES (
    'scenario_wf02',
    '{
      "default_lookback_window": "8Y",
      "default_lookback_days": 2920,
      "parse_timeout_seconds": 120,
      "run_timeout_seconds": 120,
      "phase": "phase1",
      "past_analogue_allowed": true,
      "past_analogue_archive_years": 8,
      "past_analogue_archive_days": 2920,
      "past_analogue_top_k": 5,
      "past_analogue_similarity_threshold": 0.35,
      "past_analogue_trailing_blend": 0.4,
      "past_analogue_mix": 0.5,
      "past_analogue_max_window_days": 180,
      "past_analogue_search_timeout_ms": 15000,
      "feature_flags": {
        "house_research_rag": true,
        "market_research_web": true,
        "external_research_llm": false,
        "strategist_stub_ok": false,
        "past_analogue": true,
        "past_analogue_apply": true,
        "past_analogue_web_search": true
      },
      "excel_timeouts": {
        "parse_ms": 120000,
        "params_ms": 15000,
        "run_ms": 120000,
        "pdf_ms": 30000
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

-- Sample trading rules (weight / universe / jurisdiction live here)
INSERT INTO trading_rules (rule_name, rule_category, min_value, max_value, parameters, is_active)
VALUES
    (
        'max_single_name_weight',
        'PORTFOLIO',
        NULL,
        0.25,
        '{"max_single_weight": 0.25, "unit": "weight_pct", "reason": "Max weight per name 25%"}'::jsonb,
        true
    ),
    (
        'min_cash_buffer',
        'PORTFOLIO',
        0.05,
        NULL,
        '{"min_cash_weight": 0.05, "reason": "Minimum cash buffer 5%"}'::jsonb,
        true
    ),
    (
        'approved_crypto_universe',
        'PORTFOLIO',
        NULL,
        NULL,
        '{"approved_tickers": ["BTC", "ETH", "DOGE", "XRP", "SOL"], "reason": "Approved crypto investable universe"}'::jsonb,
        true
    ),
    (
        'jurisdictions',
        'CLIENT',
        NULL,
        NULL,
        '{"sfc_enabled": true, "sec_enabled": true, "allowed_markets": ["us", "hk", "crypto"], "reason": "Firm allowed markets"}'::jsonb,
        true
    ),
    (
        'asset_class_weight_cap',
        'PORTFOLIO',
        NULL,
        NULL,
        '{"asset_class": null, "max_total_weight": null, "reason": "Template: set asset_class + max_total_weight"}'::jsonb,
        false
    )
ON CONFLICT (rule_name) DO UPDATE
SET
    rule_category = EXCLUDED.rule_category,
    parameters = EXCLUDED.parameters,
    min_value = EXCLUDED.min_value,
    max_value = EXCLUDED.max_value,
    is_active = EXCLUDED.is_active,
    updated_at = now();

-- Ensure technical ingest limit is not duplicated in trading_rules
DELETE FROM trading_rules WHERE rule_name = 'max_batch_ingest_rows';

-- Sample application_event rows for Phase-1 Streamlit demos.
-- Fixed UUIDs so docs can cite them: ask "Does log ID <uuid> violate …?"
INSERT INTO audit_events (
    id,
    source_type,
    source_name,
    event_action,
    event_type,
    advisor_id,
    actor_id,
    session_id,
    entity_refs,
    payload,
    normalized_params,
    created_at
)
VALUES
    (
        'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeee0001',
        'application_event',
        'portfolio_api_v1',
        'portfolio_update',
        'portfolio',
        '00000000-0000-4000-8000-000000000001',
        '00000000-0000-4000-8000-000000000001',
        NULL,
        '{"client_alias": "demo_client_a", "portfolio_id": "11111111-2222-4333-8444-555555555501", "ticker": "AAPL"}'::jsonb,
        '{
          "clients_updated": ["demo_client_a"],
          "positions_count": 2,
          "row_count": 2,
          "ticker_count": 2,
          "positions": [
            {"ticker": "AAPL", "asset_class": "EQUITY", "current_weight_pct": 0.72, "portfolio_id": "11111111-2222-4333-8444-555555555501"},
            {"ticker": "MSFT", "asset_class": "EQUITY", "current_weight_pct": 0.28, "portfolio_id": "11111111-2222-4333-8444-555555555501"}
          ]
        }'::jsonb,
        '{"row_count": 2, "ticker_count": 2, "event_action": "portfolio_update"}'::jsonb,
        now() - interval '1 day'
    ),
    (
        'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeee0002',
        'application_event',
        'portfolio_api_v1',
        'portfolio_update',
        'portfolio',
        '00000000-0000-4000-8000-000000000001',
        '00000000-0000-4000-8000-000000000001',
        NULL,
        '{"client_alias": "demo_client_b", "portfolio_id": "11111111-2222-4333-8444-555555555502", "ticker": "BTC-USD"}'::jsonb,
        '{
          "clients_updated": ["demo_client_b"],
          "positions_count": 1,
          "row_count": 1,
          "ticker_count": 1,
          "positions": [
            {"ticker": "BTC-USD", "asset_class": "CRYPTO", "current_weight_pct": 1.0, "portfolio_id": "11111111-2222-4333-8444-555555555502"}
          ]
        }'::jsonb,
        '{"row_count": 1, "ticker_count": 1, "event_action": "portfolio_update"}'::jsonb,
        now() - interval '2 hours'
    ),
    (
        'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeee0003',
        'agent_telemetry',
        'agent_trace_run',
        'agent_trace',
        'telemetry_placeholder',
        '00000000-0000-4000-8000-000000000001',
        '00000000-0000-4000-8000-000000000001',
        'phase2-placeholder-session',
        '{"client_alias": "demo_client_a", "ticker": "AAPL"}'::jsonb,
        '{"note": "Phase 2 placeholder — writers owned by crew_ai_coding", "confidence_score": 0.7}'::jsonb,
        '{"event_action": "agent_trace"}'::jsonb,
        now() - interval '3 hours'
    )
ON CONFLICT (id) DO NOTHING;
