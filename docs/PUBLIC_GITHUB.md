# Making this repository public — hygiene

Before flipping GitHub **Visibility → Public**, confirm nothing sensitive is in
the tree or history.

## How to flip visibility (manual)

1. Open https://github.com/noxqzez/wealth_blotter_plus
2. **Settings** → **General** → scroll to **Danger Zone**
3. **Change repository visibility** → **Make public** → confirm

The repo URL **does not change** when you go from private → public. Links you
already shared keep working; only who can *clone without an invite* changes.

## Already safe (verified pattern)

| Item | Status |
|------|--------|
| `.env` / local secrets | Gitignored; never commit |
| Service role / JWT / Resend / Admin / Tavily keys | Render / local `.env` only |
| `.env.example` / `render.yaml` | Placeholders / env **names** only |
| `frontend_excel/.env.production` | Placeholder `YOUR_API_HOST` — set real URL on Render build env |
| Committed manifests | Placeholders (`YOUR_EXCEL_HOST`, `YOUR_API_HOST`, `YOUR_PROJECT_REF`) |
| `manifest.pilot.LIVE.xml` | **Gitignored** — email to pilots only |
| Sample CSV | Synthetic `demo_client` tickers only |

## Excel manifests (split)

See [frontend_excel/MANIFESTS.md](../frontend_excel/MANIFESTS.md).

- **Git / public / self-host:** `manifest.pilot.xml` with `YOUR_*` placeholders.
- **Trusted pilots:** attach gitignored `manifest.pilot.LIVE.xml` (real Render hosts).

After placeholders land on `main`, the Static Site URL
`…/manifest.pilot.xml` also serves placeholders — pilots must use the email
attachment, not that URL.

## Must do before / when going public

1. **Do not commit** `.env`, Streamlit `secrets.toml`, certs, `_tmp*`, or `manifest.pilot.LIVE.xml`.
2. On **Render Excel Static Site**, set build env  
   `VITE_API_BASE_URL=https://wealth-blotter-plus.onrender.com` (or your API).
3. **Rotate** any key that ever appeared in a chat paste, screenshot, or old gist.
4. Optional: `gitleaks detect` before the visibility change.

## Never put in git

- `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWT_SECRET`, `ADMIN_API_KEY`, `RESEND_API_KEY`
- Pilot / provider LLM API keys
- Real client names, account numbers, or production portfolio extracts
- mkcert / TLS private keys under `frontend_excel/.certs/`
- Live pilot manifest with production hostnames (use LIVE + gitignore)

See also: [DEPLOY_PILOT.md](DEPLOY_PILOT.md), [rules/03_security_secrets.md](../rules/03_security_secrets.md).
