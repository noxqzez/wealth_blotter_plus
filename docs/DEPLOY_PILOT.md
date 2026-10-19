# Pilot production deploy checklist

**ALWAYS deploy the API / Excel static site from Git branch `main`.**  
Do **not** deploy from `develop/next` (or other experiment branches) unless you
explicitly intend a test-only preview.

Prefer tagging before cutover, e.g. `git tag pilot-v1` on the commit you ship.

Work the sections in order. Mark each box before going live.

---

## 0. Source control

- [ ] Render API + Excel Static Site both track Git branch **`main`**
- [ ] Local `main` matches `origin/main` (no unpushed deploy-critical fixes left on `develop/next`)
- [ ] Optional: `git tag pilot-v1 && git push origin pilot-v1`

---

## 1. Supabase — schema & migrations

Run in the **SQL editor** (or `psql`) against the **production** project. Prefer additive migrations over re-running full `schema.sql` on a live DB that already has data.

### Baseline (first-time project only)

- [ ] `backend/app/db/schema.sql` (or equivalent already applied)
- [ ] `backend/app/db/seed.sql` + `backend/app/db/seed_configs/*.json` as needed for `configs` / guardrails  
  - Confirm `feature_flags.past_analogue` / `past_analogue_apply` / `past_analogue_web_search` and `past_analogue_mix` match intended pilot defaults

### Pilot auth & onboarding (required)

- [ ] `backend/app/db/migration_pilot_profiles.sql` — `profiles.approval_status`, RLS, telemetry attribution
- [ ] `backend/app/db/migration_pilot_welcome_email.sql` — `welcome_email_sent_at` for Resend idempotency

### Core product workflows (required if not already in schema)

- [ ] `migration_portfolio_import_wf01.sql` (+ any follow-ups you use, e.g. HITL / drop diffs)
- [ ] `migration_scenario_wf02.sql`
- [ ] `migration_staged_orders_place.sql`
- [ ] `migration_compliance_phase1.sql` (if compliance / audit paths are live)
- [ ] `migration_client_risk_profile.sql` / `migration_market_eod_and_pos_id.sql` / `migration_performance_series.sql` / `migration_interaction_ratings.sql` — apply any your pilots will hit

### RAG (required if research-PDF upload is enabled)

- [ ] Enable extension **`vector`** (Database → Extensions)
- [ ] Create Storage bucket **`research-documents`** (or `RAG_STORAGE_BUCKET`) — **private**, not public
- [ ] `backend/app/db/migration_rag_poc.sql`
- [ ] `backend/app/db/migration_rag_upload_guards.sql` — `page_count` + daily quota columns

### Sanity

- [ ] RLS enabled on client / portfolio / trade / document tables (`advisor_id = auth.uid()`)
- [ ] No service-role key in client-facing apps; only on the API service

---

## 2. Supabase Auth

- [ ] Email/password auth enabled (pilot path: `/request-access` → Excel sign-in)
- [ ] Site URL / redirect allow-list includes API origin if you use magic links later (password path is primary for now)
- [ ] **SSO stays off** until IdP is ready (`SSO_ENABLED=false` on API) — see [SSO_EXTENSION_PLAN.md](SSO_EXTENSION_PLAN.md)

---

## 3. Resend (welcome email)

Host approve UI at `https://<api>.onrender.com/admin/pilots` sends mail via **Resend** (not a third-party “Ripple” product).

- [ ] Resend account + API key created
- [ ] From domain verified (or use Resend’s temporary onboarding sender for first tests only)
- [ ] Set on **API** (Render secrets):

| Env | Purpose |
|-----|---------|
| `ADMIN_API_KEY` | Long random; header `X-Admin-Key` for `/admin/pilots` |
| `RESEND_API_KEY` | Resend secret (server only) |
| `PILOT_WELCOME_FROM_EMAIL` | Verified From, e.g. `Wealth Blotter <pilots@yourdomain.com>` |
| `PILOT_MANIFEST_URL` | Link in email → Excel `manifest.pilot.xml` |
| `PILOT_EXCEL_BASE_URL` | Excel Static Site origin (docs / email copy) |

- [ ] Smoke: approve a test pilot → email arrives → no password in body → `welcome_email_sent_at` set

---

## 4. Render — FastAPI (Docker Web Service)

Blueprint: [`render.yaml`](../render.yaml) service `wealth-blotter-plus-api`.  
Runtime: root **`Dockerfile`** → `uvicorn backend.app.main:app`.

### Service settings

- [ ] Repo `wealth_blotter_plus`, branch **`main`**, Docker runtime
- [ ] Health check path: `/api/v1/health`
- [ ] Plan / region sized for single instance (Copilot FSM is in-memory — do not scale to N without shared session store)

### FastAPI / app env (production)

| Variable | Required value | Notes |
|----------|----------------|-------|
| `ENVIRONMENT` | `production` | Hides OpenAPI `/docs` |
| `LOG_LEVEL` | `INFO` | |
| `DEV_AUTH_BYPASS` | `false` | **Never** `true` on Render |
| `REQUIRE_PILOT_APPROVAL` | `true` | Only `APPROVED` profiles |
| `REQUIRE_BYOK` | `true` | Pilots send primary LLM headers from Excel |
| `SUPABASE_URL` | project URL | |
| `SUPABASE_ANON_KEY` | anon | |
| `SUPABASE_SERVICE_ROLE_KEY` | service role | **Secret**; API only |
| `SUPABASE_JWT_SECRET` | JWT secret | **Secret** |
| `JWT_ALGORITHM` | `HS256` | |
| `JWT_AUDIENCE` | `authenticated` | |
| `CORS_ORIGINS` | exact origins, comma-separated | See §6 — **no `*`** |
| `USE_CLOUD_LLM` | `false` | Server does not hold pilot LLM keys |
| `CONFIDENCE_THRESHOLD` | `0.85` | HITL routing |
| `SSO_ENABLED` | `false` | Keep until IdP ready |
| `SSO_REQUIRE_APPROVAL` | `true` | |
| `SSO_PROVIDERS` | empty | |
| `SSO_AUTO_APPROVE_DOMAINS` | empty | |
| `ADMIN_API_KEY` | long random | `/admin/pilots` |
| `RESEND_API_KEY` | Resend | Welcome email |
| `PILOT_WELCOME_FROM_EMAIL` | verified From | |
| `PILOT_MANIFEST_URL` | Excel manifest URL | |
| `PILOT_EXCEL_BASE_URL` | Excel Static Site URL | |

### Optional FastAPI env (features)

| Variable | When | Notes |
|----------|------|-------|
| `TAVILY_API_KEY` | Past Analogue web search (Option A) | Server-side; thrift via `TAVILY_MAX_CALLS_PER_PROCESS`, `PAST_ANALOGUE_MAX_SEARCH_QUERIES` |
| `TAVILY_CACHE_DIR` | Past Analogue | Default `artifacts/past_analogue/tavily_cache` — ensure writable on instance or accept cold cache |
| `PAST_ANALOGUE_LLM_PROVIDER` | Override extract LLM | Usually empty (uses request BYOK / build_llm path) |
| `RAG_STORAGE_BUCKET` | RAG | Default `research-documents` |
| `RAG_UPLOAD_ENABLED` | RAG | Kill switch (`false` freezes uploads) |
| `RAG_UPLOAD_RATE_LIMIT_PER_MINUTE` | RAG | Default `5` |
| `RAG_UPLOAD_MAX_PAGES_PER_DAY` | RAG | Default `50` |
| `RAG_UPLOAD_MAX_PAGES_PER_FILE` | RAG | Default `50` |
| `RAG_EMBEDDING_MODEL` / `RAG_EMBEDDING_DIMS` | RAG | Must match DB `vector(N)` (default 384 / MiniLM) |
| `PUBLIC_API_BASE_URL` | Optional | Public API URL if used in links |

### Do **not** set on Render for pilot

- Pilot `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / Gemini keys as shared server secrets (BYOK via Excel headers instead)
- `DEV_AUTH_BYPASS=true`
- `CORS_ORIGINS=*`

### Docker / RAG note

Root `Dockerfile` runs `pip install .` **without** the `[rag]` extra. If pilots must upload/embed PDFs on Render, change the image to install `.[rag]` (PyMuPDF + sentence-transformers) **before** go-live, or keep `RAG_UPLOAD_ENABLED=false` until that ships.

---

## 5. Render — Excel task pane (Static Site)

Blueprint service `wealth-blotter-plus-excel`.

- [ ] Root directory `frontend_excel`
- [ ] Build: `npm ci && npm run build` → publish `dist`
- [ ] Branch **`main`**
- [ ] Build-time env **required**: `VITE_API_BASE_URL=https://<api-service>.onrender.com` (no trailing slash).  
  Repo `.env.production` is a `YOUR_API_HOST` placeholder for public GitHub.
- [ ] Committed manifests use `YOUR_EXCEL_HOST` / `YOUR_API_HOST` / `YOUR_PROJECT_REF` — see [frontend_excel/MANIFESTS.md](../frontend_excel/MANIFESTS.md)
- [ ] Email pilots **`manifest.pilot.LIVE.xml`** (gitignored; local file with real Render hosts) — do not rely on the Static Site `…/manifest.pilot.xml` once placeholders are deployed
- [ ] After deploy, verify:
  - `…/index.html` loads
  - `…/assets/icon-32.png` returns an image
  - API calls from the pane succeed (confirms `VITE_API_BASE_URL` on Render)

---

## 6. CORS wiring (after Static Site URL is known)

On the **API** service:

```text
CORS_ORIGINS=https://localhost:3000,https://<excel-static-host>
```

- [ ] Includes hosted Excel origin
- [ ] Includes `https://localhost:3000` only if you still debug locally against prod API (optional; tighten later)
- [ ] Redeploy / restart API after CORS change

---

## 7. Security gates (final gate before announcing pilots)

| Check | Must be |
|-------|---------|
| Deploy source | **`main`** |
| `ENVIRONMENT` | `production` |
| `DEV_AUTH_BYPASS` | `false` |
| `REQUIRE_PILOT_APPROVAL` | `true` |
| `REQUIRE_BYOK` | `true` (recommended) |
| `CORS_ORIGINS` | Exact list, no `*` |
| Service role / admin / Resend keys | API env only |
| `research-documents` | Private |
| RAG quotas / kill switch | Set as intended |
| Migrations in §1 | Applied |

---

## 8. Post-deploy smoke

1. [ ] `GET /api/v1/health` → 200  
2. [ ] `/request-access` loads; register a throwaway pilot  
3. [ ] `/admin/pilots` + `ADMIN_API_KEY` → Approve & send welcome (Resend)  
4. [ ] Sideload `manifest.pilot.xml` → set primary LLM (BYOK) → sign in  
5. [ ] Import securities → scenario parse → run → `ord_blotter` + PDF  
6. [ ] `usage_metrics` / `audit_events` rows for that `advisor_id`  
7. [ ] If Past Analogue on: chat shows historical weight line; jsonb under `scenario_runs.strategy_view.past_analogue`  
8. [ ] If RAG on: upload a small PDF → job progresses (or confirm kill switch blocks cleanly)

---

## 9. Ops expectations (pilot)

- Copilot chat FSM is **in-memory** — Render restart/redeploy clears mid-chat sessions; Supabase data is durable.
- Single web instance assumed for session affinity.
- Past Analogue / search failures should fail-open where designed; live blotter still needs a successful optimize path.
- You do **not** give pilots: GitHub, service role, JWT secret, Render dashboard, or other advisors’ data.

---

## Related docs

- [PUBLIC_GITHUB.md](PUBLIC_GITHUB.md) — hygiene before making the GitHub repo public  
- [PILOT_PHASE1.md](PILOT_PHASE1.md) — host env + Render wiring narrative  
- [PILOT.md](../PILOT.md) — what pilots do  
- [RAG_POC.md](../backend/app/db/RAG_POC.md) — upload guards + bucket  
- [PAST_ANALOGUE_EVAL.md](PAST_ANALOGUE_EVAL.md) — historical match eval  
- [`.env.example`](../.env.example) — full env template  
- [`render.yaml`](../render.yaml) — Blueprint env names  
