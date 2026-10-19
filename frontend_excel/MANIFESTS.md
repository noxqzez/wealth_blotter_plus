# Excel manifests — which file for whom

| File | In git? | Audience | Hosts |
|------|---------|----------|--------|
| [`manifest.pilot.xml`](manifest.pilot.xml) | Yes | Public / self-host | `YOUR_EXCEL_HOST`, `YOUR_API_HOST`, `YOUR_PROJECT_REF` |
| [`public/manifest.pilot.xml`](public/manifest.pilot.xml) | Yes | Same (Static Site copy) | Placeholders |
| [`manifest.xml`](manifest.xml) | Yes | Local mkcert dev only | `localhost:3000` + placeholders |
| **`manifest.pilot.LIVE.xml`** | **No (gitignored)** | **Trusted pilots** | Real Render URLs |

## Pilot email attachment

1. Open `frontend_excel/manifest.pilot.LIVE.xml` on your machine (created locally; not pushed).
2. Attach it to the welcome email (or rename to `manifest.pilot.xml` when attaching if you prefer a simple name).
3. Tell pilots to sideload that file — **not** the placeholder XML from GitHub.

Pilots do **not** need Supabase service-role or JWT secrets — only request-access signup, approval, and their own LLM key.

## Render Static Site

After this change, `https://<excel-static>/manifest.pilot.xml` serves **placeholders**. That is intentional for a public-ready tree. Pilots use the LIVE attachment instead.

Keep **`VITE_API_BASE_URL=https://wealth-blotter-plus.onrender.com`** set on the Render Static Site **build** env so the JS bundle still talks to your API (do not rely on committing that hostname in `.env.production` once public).
