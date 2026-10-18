# Phase 1 Pilot — what you configure & what you give pilots

## A. Apply database migration (your Supabase project)

In the Supabase SQL editor, run:

`backend/app/db/migration_pilot_profiles.sql`

Then approve pilots by editing table **`profiles`**:
set `approval_status` = `APPROVED` for their row.

---

## B. Values for this project (local `.env`)

Copy from `.env.example`. You already have most Supabase keys.

| Variable | Local | Notes |
|----------|-------|--------|
| `ENVIRONMENT` | `development` | |
| `DEV_AUTH_BYPASS` | `true` (optional) | Local Excel without login |
| `REQUIRE_BYOK` | `false` | Local can use Ollama |
| `REQUIRE_PILOT_APPROVAL` | `false` | Until migration applied |
| `SUPABASE_URL` | your project URL | |
| `SUPABASE_ANON_KEY` | anon key | |
| `SUPABASE_SERVICE_ROLE_KEY` | **service role** | Server only — never to pilots |
| `SUPABASE_JWT_SECRET` | JWT secret from Project Settings → API | |
| `CORS_ORIGINS` | `https://localhost:3000,...` | Add Excel HTTPS origin |

Excel local (developers only):

```bash
# frontend_excel/.env
VITE_API_BASE_URL=http://localhost:8000
```

Pilots do **not** run the local Vite server.

---

## C. Render — API (Web Service) + Excel pane (Static Site)

### C1. API Web Service

On [Render’s new Web Service](https://dashboard.render.com/web/new) (or via [`render.yaml`](../render.yaml)):

1. **GitHub repo** → `wealth_blotter_plus`
2. **Language / runtime** → **Docker** (uses root `Dockerfile`)
3. **Branch** → `main`
4. **Name / region / plan**

Then open **Environment** and set:

| Variable | Value |
|----------|--------|
| `ENVIRONMENT` | `production` |
| `DEV_AUTH_BYPASS` | `false` |
| `REQUIRE_BYOK` | `true` |
| `REQUIRE_PILOT_APPROVAL` | `true` |
| `SUPABASE_URL` | same as local |
| `SUPABASE_ANON_KEY` | same as local |
| `SUPABASE_SERVICE_ROLE_KEY` | same as local (**secret**) |
| `SUPABASE_JWT_SECRET` | same as local (**secret**) |
| `CORS_ORIGINS` | see C3 below |
| `JWT_AUDIENCE` | `authenticated` |
| `JWT_ALGORITHM` | `HS256` |
| `USE_CLOUD_LLM` | `false` |
| `CONFIDENCE_THRESHOLD` | `0.85` |
| `LOG_LEVEL` | `INFO` |
| `SSO_ENABLED` | `false` (**keep false** for now) |
| `SSO_REQUIRE_APPROVAL` | `true` |
| `SSO_PROVIDERS` | leave empty until enabling SSO |
| `SSO_AUTO_APPROVE_DOMAINS` | leave empty |

SSO code is shipped but **disabled**. Password + `/request-access` remain the only onboarding path until you deliberately enable SSO (see [SSO_EXTENSION_PLAN.md](SSO_EXTENSION_PLAN.md)).

**LLM provider is flexible — do not hardcode OpenAI on Render.**  
Each pilot picks **OpenAI or Anthropic** in the Excel task pane (BYOK). The server uses `X-LLM-Provider` + `X-LLM-API-Key` per request. You do **not** need `LLM_PROVIDER`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY` on Render for the pilot.

Health check path: `/api/v1/health`

After deploy, note your public API URL: `https://<api-service>.onrender.com`  
(example live: `https://wealth-blotter-plus.onrender.com`)

**Shareable pilot signup:** `https://<api-service>.onrender.com/request-access`  
(root `/` redirects there). Pilots register in the browser; approve in `profiles`, then they sign in from Excel.

### C2. Excel task pane — Static Site

Create a **Static Site** (Blueprint service `wealth-blotter-plus-excel` in [`render.yaml`](../render.yaml)):

| Setting | Value |
|---------|--------|
| Root directory | `frontend_excel` |
| Build command | `npm ci && npm run build` |
| Publish directory | `dist` |
| Env `VITE_API_BASE_URL` | `https://<your-api-service>.onrender.com` (no trailing slash) |

Expected hostname if you keep the Blueprint name:  
`https://wealth-blotter-plus-excel.onrender.com`

After deploy, verify:

- `https://<excel-static>/index.html` loads the task pane UI  
- `https://<excel-static>/manifest.pilot.xml` downloads  
- `https://<excel-static>/assets/icon-32.png` returns an image  

If your Static Site hostname differs, update URLs in:

- [`frontend_excel/manifest.pilot.xml`](../frontend_excel/manifest.pilot.xml)
- [`frontend_excel/public/manifest.pilot.xml`](../frontend_excel/public/manifest.pilot.xml)  
then redeploy the Static Site.

### C3. CORS (required for hosted pane)

On the **API** service, set `CORS_ORIGINS` to include the Static Site origin (and local Excel for your own debugging):

```text
https://localhost:3000,https://wealth-blotter-plus-excel.onrender.com
```

Replace the Static Site hostname if yours differs. Restart/redeploy the API after changing CORS.

Do **not** put a Render API key in this app’s `.env`.

---

## D. What to give each pilot user

1. **Request access URL**: `https://<api-service>.onrender.com/request-access`
2. **Manifest download URL**: `https://<excel-static>.onrender.com/manifest.pilot.xml`  
   (or attach the file)
3. Point them at **[PILOT.md](../PILOT.md)** — short walkthrough (no GitHub / Node / mkcert)

You do **not** give them: GitHub access, service role key, JWT secret, Render credentials, or other pilots’ data.

---

## E. Your admin workflow

1. Pilot registers → row in `profiles` with `PENDING_APPROVAL`.
2. **Prefer:** open **`https://<api-service>.onrender.com/admin/pilots`**, enter `ADMIN_API_KEY` + pilot email → **Approve & send welcome**.  
   That sets `APPROVED` and emails the pilot the manifest link, setup steps, and a starter CSV.  
   (Apply `backend/app/db/migration_pilot_welcome_email.sql` once for `welcome_email_sent_at`.)
3. **Alternative:** set `approval_status = APPROVED` in Supabase Table Editor, then use `/admin/pilots` with **Force resend** if you still need the welcome email.
4. Pilot sideloads `manifest.pilot.xml` **once**, opens the pane, pastes BYOK, signs in.
5. JWT works; `usage_metrics` rows appear with their `advisor_id`.
6. Troubleshoot via `usage_metrics` / `audit_events` filtered by `advisor_id`.

### Welcome email (Resend)

| Env on API | Purpose |
|------------|---------|
| `ADMIN_API_KEY` | Secret for `/admin/pilots` (header `X-Admin-Key`) |
| `RESEND_API_KEY` | [Resend](https://resend.com) API key |
| `PILOT_WELCOME_FROM_EMAIL` | Verified From, e.g. `Wealth Blotter <pilots@yourdomain.com>` |
| `PILOT_MANIFEST_URL` | Link included in the email |

The email **never** includes the pilot’s password. Send is idempotent unless you check **Force resend**.

---

## F. Developer local Excel (optional)

Developers still use localhost HTTPS:

```bash
cd frontend_excel
npm install
npm run certs   # once
npm run dev     # https://localhost:3000
```

Sideload [`frontend_excel/manifest.xml`](../frontend_excel/manifest.xml) (localhost), not `manifest.pilot.xml`.

---

## G. Quick API smoke (optional)

```bash
# Register
curl -s -X POST "$API/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d '{"email":"pilot@example.com","password":"password123"}'

# After you APPROVE in Supabase:
curl -s -X POST "$API/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"email":"pilot@example.com","password":"password123"}'
```
