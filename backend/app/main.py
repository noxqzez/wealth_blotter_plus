"""FastAPI entry point — CORS, middleware, router mounting."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.app.api.v1.router import api_v1_router
from backend.app.config import get_settings
from backend.app.core.rules_engine import RulesEngineError, get_rules_engine

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Best-effort rules cache warm; non-fatal if Supabase is not configured yet."""
    try:
        engine = get_rules_engine()
        await engine.refresh()
        logger.info("rules_cache_warmed")
    except Exception as exc:  # noqa: BLE001 — scaffold: allow boot without DB
        logger.warning("rules_cache_warmup_skipped", error=str(exc))
    yield


app = FastAPI(
    title="Wealth Blotter Plus",
    version="0.1.0",
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Correlation-ID"],
    expose_headers=["X-Request-ID", "X-Process-Time-Ms"],
)


@app.middleware("http")
async def request_timing_and_audit_hooks(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Bind correlation id, record latency."""
    correlation_id = (
        request.headers.get("X-Request-ID")
        or request.headers.get("X-Correlation-ID")
        or str(uuid4())
    )
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
    request.state.correlation_id = correlation_id

    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        structlog.contextvars.clear_contextvars()
        raise
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.2f}"
    response.headers["X-Request-ID"] = correlation_id
    logger.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        latency_ms=round(elapsed_ms, 2),
    )
    return response


@app.exception_handler(RulesEngineError)
async def rules_engine_exception_handler(
    _request: Request,
    exc: RulesEngineError,
) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


app.include_router(api_v1_router, prefix="/api/v1")
