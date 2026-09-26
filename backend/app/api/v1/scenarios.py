"""WF-02 market scenario endpoints (Excel-facing).

Timeouts (enforced with asyncio.wait_for; also documented in configs.scenario_wf02):
  POST /parse  ~120s (local Ollama)
  PUT  /params  ~15s
  POST /run    ~120s
  POST /pdf     ~30s
"""

from __future__ import annotations

import asyncio
import time
from io import BytesIO
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from backend.app.core.error_codes import (
    WBP_SCENARIO_PARAMS,
    WBP_SCENARIO_PARSE,
    WBP_SCENARIO_PDF,
    WBP_SCENARIO_RUN,
    WBP_SCENARIO_TIMEOUT,
    error_payload,
)
from backend.app.core.rules_engine import RulesEngine, get_rules_engine
from backend.app.core.security import AuthContext, get_current_user
from backend.app.schemas.scenario import (
    ScenarioParseRequest,
    ScenarioParseResponse,
    ScenarioPdfRequest,
    ScenarioRunRequest,
    ScenarioRunResponse,
    ScenarioUpdateParamsRequest,
    ScenarioUpdateParamsResponse,
)
from backend.app.services.scenario_run import (
    PARAMS_TIMEOUT_SECONDS,
    PARSE_TIMEOUT_SECONDS,
    PDF_TIMEOUT_SECONDS,
    RUN_TIMEOUT_SECONDS,
    ScenarioServiceError,
    build_pdf_bytes,
    execute_scenario_run,
    mark_run_failed,
    parse_and_create_run,
    record_usage_metric,
    update_scenario_params,
)

router = APIRouter()
logger = structlog.get_logger(__name__)


def _correlation_id(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", "") or "")


def _http_from_service(
    exc: ScenarioServiceError,
    *,
    correlation_id: str,
) -> HTTPException:
    support = exc.http_status >= 500
    return HTTPException(
        status_code=exc.http_status,
        detail=error_payload(
            error_code=exc.error_code,
            message=str(exc),
            correlation_id=correlation_id or None,
            support=support,
        ),
    )


def _http_timeout(
    *,
    operation: str,
    timeout_seconds: int,
    correlation_id: str,
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        detail=error_payload(
            error_code=WBP_SCENARIO_TIMEOUT,
            message=(
                f"Scenario {operation} exceeded {timeout_seconds}s timeout "
                f"(see configs.scenario_wf02 / WF-02 locked timeouts)"
            ),
            correlation_id=correlation_id or None,
            support=False,
        ),
    )


def _http_server(
    exc: Exception,
    *,
    error_code: str,
    correlation_id: str,
) -> HTTPException:
    logger.exception(
        "scenario_api_server_error",
        error=str(exc),
        error_code=error_code,
        correlation_id=correlation_id,
    )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=error_payload(
            error_code=error_code,
            message=str(exc),
            correlation_id=correlation_id or None,
            support=True,
        ),
    )


@router.post("/parse", response_model=ScenarioParseResponse)
async def scenarios_parse(
    payload: ScenarioParseRequest,
    request: Request,
    user: AuthContext = Depends(get_current_user),
) -> ScenarioParseResponse:
    """NL prompt → scenario_params; creates scenario_runs (status PARSED)."""
    # Timeout: PARSE_TIMEOUT_SECONDS (~30s)
    cid = _correlation_id(request)
    try:
        return await asyncio.wait_for(
            parse_and_create_run(
                portfolio_id=payload.portfolio_id,
                user_prompt=payload.user_prompt,
                advisor_id=user.advisor_id,
            ),
            timeout=PARSE_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise _http_timeout(
            operation="parse",
            timeout_seconds=PARSE_TIMEOUT_SECONDS,
            correlation_id=cid,
        ) from exc
    except ScenarioServiceError as exc:
        raise _http_from_service(exc, correlation_id=cid) from exc
    except Exception as exc:  # noqa: BLE001
        raise _http_server(exc, error_code=WBP_SCENARIO_PARSE, correlation_id=cid) from exc


@router.put("/params", response_model=ScenarioUpdateParamsResponse)
async def scenarios_update_params(
    payload: ScenarioUpdateParamsRequest,
    request: Request,
    user: AuthContext = Depends(get_current_user),
) -> ScenarioUpdateParamsResponse:
    """Human-edited params → diffs + scenario_params_version bump."""
    # Timeout: PARAMS_TIMEOUT_SECONDS (~15s)
    cid = _correlation_id(request)
    try:
        return await asyncio.wait_for(
            update_scenario_params(
                scenario_run_id=payload.scenario_run_id,
                human_params=payload.scenario_params,
                advisor_id=user.advisor_id,
            ),
            timeout=PARAMS_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise _http_timeout(
            operation="params",
            timeout_seconds=PARAMS_TIMEOUT_SECONDS,
            correlation_id=cid,
        ) from exc
    except ScenarioServiceError as exc:
        raise _http_from_service(exc, correlation_id=cid) from exc
    except Exception as exc:  # noqa: BLE001
        raise _http_server(
            exc, error_code=WBP_SCENARIO_PARAMS, correlation_id=cid
        ) from exc


@router.post("/run", response_model=ScenarioRunResponse)
async def scenarios_run(
    payload: ScenarioRunRequest,
    request: Request,
    user: AuthContext = Depends(get_current_user),
    rules: RulesEngine = Depends(get_rules_engine),
) -> ScenarioRunResponse:
    """
    Full Phase-1 pipeline (research stub ∥ risk → optimize → stage → content).

    Pipeline failures return HTTP 200 with status=FAILED + error_code.
    Missing migration / auth-scoped not-found raise HTTPException.
    """
    # Timeout: RUN_TIMEOUT_SECONDS (~120s)
    cid = _correlation_id(request)
    limits = await rules.get_technical_limits()
    try:
        return await asyncio.wait_for(
            execute_scenario_run(
                scenario_run_id=payload.scenario_run_id,
                advisor_id=user.advisor_id,
                limits=limits,
            ),
            timeout=RUN_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        # Best-effort audit + FAILED status (portfolio_id from run row if available)
        try:
            from backend.app.core.supabase import get_service_supabase

            row = (
                get_service_supabase()
                .table("scenario_runs")
                .select("portfolio_id")
                .eq("id", str(payload.scenario_run_id))
                .limit(1)
                .execute()
            )
            pid = (
                UUID(str(row.data[0]["portfolio_id"]))
                if row.data and row.data[0].get("portfolio_id")
                else None
            )
        except Exception:  # noqa: BLE001
            pid = None
        mark_run_failed(
            payload.scenario_run_id,
            error_code=WBP_SCENARIO_TIMEOUT,
            error_message=f"Scenario run exceeded {RUN_TIMEOUT_SECONDS}s timeout",
            portfolio_id=pid,
            advisor_id=user.advisor_id,
        )
        raise _http_timeout(
            operation="run",
            timeout_seconds=RUN_TIMEOUT_SECONDS,
            correlation_id=cid,
        ) from exc
    except ScenarioServiceError as exc:
        # SERVER/migration (503) or client errors before pipeline return
        raise _http_from_service(exc, correlation_id=cid) from exc
    except Exception as exc:  # noqa: BLE001
        raise _http_server(exc, error_code=WBP_SCENARIO_RUN, correlation_id=cid) from exc


@router.post("/pdf")
async def scenarios_pdf(
    payload: ScenarioPdfRequest,
    request: Request,
    user: AuthContext = Depends(get_current_user),
) -> StreamingResponse:
    """Download scenario PDF (application/pdf StreamingResponse)."""
    # Timeout: PDF_TIMEOUT_SECONDS (~30s)
    cid = _correlation_id(request)
    started = time.perf_counter()
    try:
        pdf_bytes, filename, client_alias = await asyncio.wait_for(
            build_pdf_bytes(
                scenario_run_id=payload.scenario_run_id,
                advisor_id=user.advisor_id,
            ),
            timeout=PDF_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise _http_timeout(
            operation="pdf",
            timeout_seconds=PDF_TIMEOUT_SECONDS,
            correlation_id=cid,
        ) from exc
    except ScenarioServiceError as exc:
        raise _http_from_service(exc, correlation_id=cid) from exc
    except Exception as exc:  # noqa: BLE001
        raise _http_server(exc, error_code=WBP_SCENARIO_PDF, correlation_id=cid) from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    record_usage_metric(
        event_action="scenario_pdf_download",
        endpoint_or_component="/api/v1/scenarios/pdf",
        client_alias=client_alias,
        execution_time_ms=elapsed_ms,
        metadata={
            "scenario_run_id": str(payload.scenario_run_id),
            "byte_length": len(pdf_bytes),
            "correlation_id": cid,
        },
    )

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Scenario-Run-Id": str(payload.scenario_run_id),
    }
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers=headers,
    )
