# SSO Extension Plan (Phase 1.5)

**Trigger phrase:** `build SSO extension`  
**Status:** Deferred — email/password pilot first  
**Depends on:** Phase 1 pilot auth (Supabase JWT + `profiles` approval + BYOK) already shipped

## Goal

Add optional corporate SSO (OAuth/OIDC/SAML via Supabase) for less tech-savvy firm users, while keeping email/password as the primary path. Ship behind a **default-off** flag so SSO can be built, tested in staging, then left disabled in production without weakening pilot security.

## Non-goals (initial SSO slice)

- Replacing email/password registration/login
- Firm-managed LLM keys (BYOK stays as-is unless separately requested)
- Auto-approving every SSO user without admin/domain rules
- Custom SAML SP implementation outside Supabase

## Architecture (fits current code)

```text
IdP (Google / Entra / Okta)
  → Supabase Auth (OAuth or SAML)
  → access_token (same JWT shape as password login)
  → Excel: Authorization: Bearer <token>
  → get_current_user (security.py)
  → require_approved_profile (pilot_auth.py)
  → existing API + BYOK header
```

**Reuse (do not duplicate):**
- `backend/app/core/security.py` — JWT decode + `AuthContext`
- `backend/app/services/pilot_auth.py` — `require_approved_profile` / `fetch_profile`
- `profiles.approval_status` gate
- `X-LLM-API-Key` BYOK context (unchanged)
- `/request-access` page (password signup stays; SSO button only when enabled)

**Add:**
- Supabase-hosted OAuth/SAML start + callback
- Excel UI: “Sign in with SSO” (hidden when disabled)
- Optional SSO button on `/request-access` when enabled
- Config flags + allowlist policy

## Feature flags (default secure)

| Variable | Default | Meaning |
|----------|---------|---------|
| `SSO_ENABLED` | `false` | Master switch; hide UI; reject SSO routes if any |
| `SSO_PROVIDERS` | empty | e.g. `azure,google` — only listed providers allowed |
| `SSO_AUTO_APPROVE_DOMAINS` | empty | Optional; e.g. `firm.com` |
| `SSO_REQUIRE_APPROVAL` | `true` | Keep manual `APPROVED` unless domain auto-approve is on |

Production Render: leave `SSO_ENABLED=false` and IdP providers **disabled** in Supabase until a real corporate client needs them.

## Implementation steps (when building)

1. **Config** — Add flags to `backend/app/config.py` + `.env.example` + `docs/PILOT_PHASE1.md` / `render.yaml` (names only).
2. **Supabase** — Document enabling Google / Azure AD / SAML; set redirect URLs for Excel dialog + `/request-access`.
3. **Profile bootstrap** — On first SSO JWT, if no `profiles` row: create `PENDING_APPROVAL` (same as register).
4. **API (thin)** — `GET /auth/sso/status` → `{ enabled, providers }` for Excel / request-access page.
5. **Excel** — SSO via `Office.context.ui.displayDialogAsync` (or documented fallback); reuse Bearer + BYOK.
6. **Tests** — Flag off inert; flag on mocked JWT pending/approved paths; no LLM key in logs.
7. **Docs** — Update `PILOT.md`; keep this file as the build checklist.

## Security checklist (before enabling in prod)

- [ ] `SSO_ENABLED=false` by default in prod
- [ ] Supabase providers disabled when flag is off
- [ ] Redirect URIs allowlist-only
- [ ] Still require `profiles.APPROVED` (or documented domain allowlist)
- [ ] No IdP client secrets in git / Excel / agent traces
- [ ] Same RLS + `advisor_id` isolation as password pilots
- [ ] BYOK still never persisted server-side

## Acceptance criteria

- Password register/login and `/request-access` unchanged when SSO is off
- Staging SSO yields same JWT path and approval gate
- Prod can ship code with SSO built but disabled
- Saying **“build SSO extension”** means: implement this plan; do not enable in prod unless asked
