"""Aggregate v1 routers."""

from fastapi import APIRouter

from backend.app.api.v1 import (
    admin_pilots,
    advisory,
    auth_pilot,
    compliance,
    copilot,
    documents,
    health,
    ingest,
    portfolio,
    portfolios,
    scenarios,
)

api_v1_router = APIRouter()
api_v1_router.include_router(health.router, tags=["health"])
api_v1_router.include_router(auth_pilot.router, prefix="/auth", tags=["auth"])
api_v1_router.include_router(
    admin_pilots.router, prefix="/admin/pilots", tags=["admin-pilots"]
)
api_v1_router.include_router(ingest.router, prefix="/ingest", tags=["ingest"])
api_v1_router.include_router(advisory.router, prefix="/advisory", tags=["advisory"])
api_v1_router.include_router(portfolio.router, prefix="/portfolio", tags=["portfolio"])
api_v1_router.include_router(
    portfolios.router, prefix="/portfolios", tags=["portfolios"]
)
api_v1_router.include_router(
    compliance.router, prefix="/compliance", tags=["compliance"]
)
api_v1_router.include_router(
    documents.router, prefix="/documents", tags=["documents"]
)
api_v1_router.include_router(
    scenarios.router, prefix="/scenarios", tags=["scenarios"]
)
api_v1_router.include_router(copilot.router, prefix="/copilot", tags=["copilot"])