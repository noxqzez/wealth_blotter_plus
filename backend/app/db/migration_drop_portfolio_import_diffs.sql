-- Drop unused WF-01 import accuracy diffs (normalization is deterministic Python;
-- user-facing messages/warnings live on audit_events instead).

DROP TABLE IF EXISTS portfolio_import_diffs;
