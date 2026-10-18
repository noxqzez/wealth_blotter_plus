# Pilot deploy

- **Always deploy from `main`**, not `develop/next`.
- Full checklist: [docs/DEPLOY_PILOT.md](../docs/DEPLOY_PILOT.md)
- Security gates: `ENVIRONMENT=production`, `DEV_AUTH_BYPASS=false`, pilot approval on, tight CORS, service-role server-only, RAG upload quotas / kill switch.
