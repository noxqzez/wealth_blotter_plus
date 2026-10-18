# Pilot golden-path E2E

API / Copilot contract tests with **mocked** Supabase, LLM, Yahoo, Tavily, and Resend.
No live network or secrets required.

## Run

```bash
# E2E only
uv run pytest -m e2e

# Full suite (unit + golden + e2e)
uv run pytest
```

## Coverage

| Step | What is asserted |
|------|------------------|
| Health | `GET /api/v1/health` |
| Auth gates | Unauthenticated portfolio → 401/403; register shape; BYOK prod gate |
| Import | validate-import → build-base (fake Supabase + quotes) |
| Scenario | parse → params → run (incl. `past_analogue`) → PDF |
| Copilot | session → process → enrich → scenario → optimize → place |
| RAG | upload kill switch → HTTP 403 |
| Admin | `X-Admin-Key` required; approve-and-welcome success |

These tests live on `develop/next` to protect refactors; they do not replace Excel desktop smoke.
