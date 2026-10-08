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

Excel local:

```bash
# frontend_excel/.env
VITE_API_BASE_URL=http://localhost:8000
```

---

## C. Values for Render (dashboard Environment — not in git)

### Creating the service (`/web/new`)

On [Render’s new Web Service](https://dashboard.render.com/web/new) onboarding you mainly pick:

1. **GitHub repo** → `wealth_blotter_plus`
2. **Language / runtime** → **Docker** (uses root `Dockerfile`)
3. **Branch** → `main`
4. **Name / region / plan**

You often **won’t** see the full env list on that first screen. Finish creating the service, then open it → **Environment** and add Key/Value rows there (add one variable at a time).

If onboarding shows a single “API key” field, **skip it** — that is not our app config. Our secrets are normal env vars (`SUPABASE_*`, etc.).

### Environment variables (after the service exists)

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
| `CORS_ORIGINS` | Excel origin(s), e.g. `https://localhost:3000` (and any hosted task-pane URL) |
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

**Do not** put a Render API key in this app’s `.env`.

Health check path: `/api/v1/health`

After deploy, note your public URL: `https://<service>.onrender.com`

**Shareable pilot signup:** `https://<service>.onrender.com/request-access`  
(root `/` redirects there). Pilots register in the browser; approve in `profiles`, then they sign in from Excel.

---

## D. What to give each pilot user

1. **Request access URL**: `https://<your-render-service>.onrender.com/request-access`
2. **Invite to your private GitHub repo** (Excel add-in).
3. **API base URL** for `VITE_API_BASE_URL`: `https://<your-render-service>.onrender.com`
4. Point them at **[PILOT.md](../PILOT.md)** — detailed non-technical steps for signup, Mac/Excel setup, BYOK, and sign-in.

You do **not** give them: service role key, JWT secret, Render credentials, or other pilots’ data.

---

## E. Your admin workflow

1. Pilot registers → row in `profiles` with `PENDING_APPROVAL`.
2. You set `approval_status = APPROVED` in Supabase Table Editor.
3. Pilot signs in → JWT works; `usage_metrics` rows appear with their `advisor_id`.
4. Troubleshoot via `usage_metrics` / `audit_events` filtered by `advisor_id`.

---

## F. Quick API smoke (optional)

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
