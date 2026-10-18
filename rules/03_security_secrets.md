# Security & Secrets

## Data isolation

- Enable and verify PostgreSQL RLS on every table with client, portfolio, or trade data
- SQL tables/columns: `snake_case`; explicit foreign keys; RLS mandatory
- v1 RLS: `advisor_id = auth.uid()`; keep `firm_id` columns for future firm rollups

## Zero credential exposure

- Never hardcode, commit, log, chat, or put secrets in agent traces (keys, tokens, DB URLs, service keys)
- Load secrets only from server-side `.env` or a managed vault
- Strip secrets from client code and agent execution traces
- Prefer pre-commit / secret scanners to block accidental exposure
- Before making the GitHub repo **public**, follow [docs/PUBLIC_GITHUB.md](../docs/PUBLIC_GITHUB.md)
- User logins: Supabase Auth (`auth.users`) only — no custom password tables
- Third-party API keys: `advisor_credentials` via Vault / pgcrypto — never return raw secrets in API responses
- Anonymize client names and account numbers before any external LLM payload

## Input sanitization

- Sanitize all dynamic user input and LLM output rendered in Streamlit or the Excel task pane
- Eliminate DOM-based XSS vectors before any HTML/JS injection into UI surfaces
