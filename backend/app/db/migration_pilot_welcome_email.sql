-- Pilot welcome email tracking (idempotent send after approval).
-- Apply on Supabase SQL editor (or migration runner).

ALTER TABLE profiles
    ADD COLUMN IF NOT EXISTS welcome_email_sent_at TIMESTAMPTZ;

COMMENT ON COLUMN profiles.welcome_email_sent_at IS
    'When the one-time pilot welcome email was sent (manifest + setup steps). NULL = not sent.';
