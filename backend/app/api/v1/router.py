"""Aggregate v1 routers."""

from fastapi import APIRouter

from backend.app.api.v1 import advisory, health, ingest

api_v1_router = APIRouter()
api_v1_router.include_router(health.router, tags=["health"])
api_v1_router.include_router(ingest.router, prefix="/ingest", tags=["ingest"])
api_v1_router.include_router(advisory.router, prefix="/advisory", tags=["advisory"])
