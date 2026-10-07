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

Create a Render **Web Service** from this repo (Docker). Set:

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

**Do not set** pilot LLM keys on Render.  
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
4. Point them at **[PILOT.md](../PILOT.md)** for Excel + BYOK after approval.

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
