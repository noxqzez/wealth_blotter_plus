-- Destructive cutover from scaffold → MVP tables (demo DB only).
-- Drops unused scaffold objects, then expects schema.sql + seed.sql.

DROP TABLE IF EXISTS agent_output_reviews CASCADE;
DROP TABLE IF EXISTS app_usage_events CASCADE;
DROP TABLE IF EXISTS advisor_credentials CASCADE;
DROP TABLE IF EXISTS audit_logs CASCADE;
-- Dropped for MVP. When re-adding staged_trades, require a unique trade identifier
-- (trade_id / trade_ref) for user/system/compliance citation — see note in schema.sql.
DROP TABLE IF EXISTS staged_trades CASCADE;
DROP TABLE IF EXISTS portfolios CASCADE;
DROP TABLE IF EXISTS clients CASCADE;
DROP TABLE IF EXISTS advisors CASCADE;
DROP TABLE IF EXISTS firms CASCADE;
DROP TABLE IF EXISTS agent_simulations CASCADE;
DROP TABLE IF EXISTS portfolio_risk_metrics CASCADE;
DROP TABLE IF EXISTS market_price_history CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS agent_overrides CASCADE;
DROP TABLE IF EXISTS usage_metrics CASCADE;
DROP TABLE IF EXISTS trading_rules CASCADE;
DROP TABLE IF EXISTS portfolio_positions CASCADE;
-- keep configs if present; recreated by schema.sql IF NOT EXISTS

DROP FUNCTION IF EXISTS deny_audit_mutation() CASCADE;
