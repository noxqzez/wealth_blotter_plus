# Making this repository public — hygiene

Before flipping GitHub **Visibility → Public**, confirm nothing sensitive is in
the tree or history.

## Already safe (verified pattern)

| Item | Status |
|------|--------|
| `.env` / `frontend_excel/.env` | Gitignored; never committed in this repo’s history |
| `SUPABASE_SERVICE_ROLE_KEY`, JWT secrets, Resend, Tavily, Admin keys | Live only in local `.env` / Render dashboard — not in git |
| `.env.example` / `render.yaml` | Placeholders / env **names** only |
| `frontend_excel/.env.production` | Public API hostname only (`VITE_API_BASE_URL`) — not a secret |
| Sample CSV | Synthetic `demo_client` tickers only |

## Must do before / when going public

1. **Do not commit** `.env`, Streamlit `secrets.toml`, certs, or `_tmp*` scratch files.
2. **Excel manifests** use `https://YOUR_PROJECT_REF.supabase.co` as `AppDomain`.  
   Before pilot sideload, set your real Supabase URL in:
   - `frontend_excel/manifest.pilot.xml`
   - `frontend_excel/public/manifest.pilot.xml`  
   Prefer keeping that substitution on a **private** deploy branch or Render build step if you want the public default to stay generic.
3. **Rotate** any key that ever appeared in a chat paste, screenshot, or old private gist (service role, JWT secret, Resend, Admin, Tavily).
4. **Local `JWT_SECRET_KEY`**: if still `change_me_use_long_random_string`, replace with a long random value for any real environment (example file intentionally uses that placeholder).
5. Optional: run a secret scanner (`gitleaks detect`) on the repo before the visibility change.

## Not secrets (but identifiable)

- Render hostnames (`*.onrender.com`) in manifests — public service URLs
- Demo UUID `00000000-0000-4000-8000-000000000001` — local DEV bypass advisor id
- Supabase **project ref** in AppDomain — not a credential; still project-specific (use placeholder in the public default)

## Never put in git

- `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWT_SECRET`, `ADMIN_API_KEY`, `RESEND_API_KEY`
- Pilot / provider LLM API keys
- Real client names, account numbers, or production portfolio extracts
- mkcert / TLS private keys under `frontend_excel/.certs/`

See also: [DEPLOY_PILOT.md](DEPLOY_PILOT.md), [rules/03_security_secrets.md](../rules/03_security_secrets.md).
