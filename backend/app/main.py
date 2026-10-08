"""FastAPI entry point — CORS, middleware, router mounting."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from jose import JWTError, jwt

from backend.app.api.v1.router import api_v1_router
from backend.app.config import get_settings
from backend.app.core.byok import clear_byok, extract_byok_from_request, set_byok
from backend.app.core.rules_engine import RulesEngineError, get_rules_engine
from backend.app.services.usage_telemetry import record_usage_metric

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_REQUEST_ACCESS_HTML = _STATIC_DIR / "request_access.html"

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

_TELEMETRY_SKIP_PREFIXES = (
    "/api/v1/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/request-access",
)
_TELEMETRY_SKIP_EXACT = frozenset({"/"})


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
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        "X-Correlation-ID",
        "X-LLM-API-Key",
        "X-LLM-Provider",
    ],
    expose_headers=["X-Request-ID", "X-Process-Time-Ms"],
)


def _advisor_id_from_bearer(request: Request) -> UUID | None:
    auth = request.headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    if not token or not settings.supabase_jwt_secret:
        return None
    try:
        claims = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            options={"verify_aud": True},
        )
        sub = claims.get("sub")
        return UUID(str(sub)) if sub else None
    except (JWTError, ValueError):
        return None


@app.middleware("http")
async def request_timing_and_audit_hooks(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Bind correlation id, BYOK context, latency + usage telemetry."""
    correlation_id = (
        request.headers.get("X-Request-ID")
        or request.headers.get("X-Correlation-ID")
        or str(uuid4())
    )
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
    request.state.correlation_id = correlation_id

    set_byok(extract_byok_from_request(request))
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        clear_byok()
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

    path = request.url.path
    skip_telemetry = path in _TELEMETRY_SKIP_EXACT or any(
        path.startswith(p) for p in _TELEMETRY_SKIP_PREFIXES
    )
    if request.method != "OPTIONS" and not skip_telemetry:
        advisor_id = _advisor_id_from_bearer(request)
        if advisor_id is not None or path.startswith("/api/v1/auth/"):
            record_usage_metric(
                event_action=f"{request.method}:{path}",
                endpoint_or_component=path,
                advisor_id=advisor_id,
                execution_time_ms=int(elapsed_ms),
                metadata={
                    "status_code": response.status_code,
                    "method": request.method,
                },
            )

    clear_byok()
    return response


@app.exception_handler(RulesEngineError)
async def rules_engine_exception_handler(
    _request: Request,
    exc: RulesEngineError,
) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Return JSON on unexpected errors so the request-access page can show a hint."""
    from backend.app.core.supabase import SupabaseNotConfiguredError

    if isinstance(exc, SupabaseNotConfiguredError):
        logger.error("supabase_not_configured", path=request.url.path, error=str(exc))
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "error_code": "WBP-AUTH-REGISTER",
                    "message": (
                        "Supabase is not configured on this server. "
                        "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY on Render."
                    ),
                }
            },
        )
    logger.exception(
        "unhandled_exception",
        path=request.url.path,
        error_type=type(exc).__name__,
        error=str(exc),
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "error_code": "WBP-INTERNAL",
                "message": f"Internal error ({type(exc).__name__}). Check Render logs.",
            }
        },
    )


app.include_router(api_v1_router, prefix="/api/v1")


@app.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    """Share the API base URL — lands on the pilot request-access form."""
    return RedirectResponse(url="/request-access", status_code=302)


@app.get("/request-access", include_in_schema=False)
async def request_access_page() -> FileResponse:
    """Public pilot registration page (no secrets; posts to /api/v1/auth/register)."""
    if not _REQUEST_ACCESS_HTML.is_file():
        raise HTTPException(status_code=404, detail="Request-access page not found")
    return FileResponse(
        path=_REQUEST_ACCESS_HTML,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )
