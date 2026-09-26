"""Excel Copilot FSM API (main workflow chat; separate from compliance chat)."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status

from backend.app.core.error_codes import (
    WBP_IMPORT_SERVER,
    WBP_SCENARIO_CLIENT,
    WBP_SCENARIO_SERVER,
    error_payload,
)
from backend.app.core.rules_engine import RulesEngine, get_rules_engine
from backend.app.core.security import AuthContext, get_current_user
from backend.app.schemas.copilot import (
    CopilotActionRequest,
    CopilotAgentResponse,
    CopilotMessageRequest,
    CopilotSessionCreateResponse,
)
from backend.app.services import copilot_session as sessions
from backend.app.services.copilot_fsm import (
    CopilotFsmError,
    build_welcome_response,
    handle_action,
    handle_message,
)
from backend.app.services.copilot_session import CopilotSessionError

router = APIRouter()
logger = structlog.get_logger(__name__)


def _correlation_id(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", "") or "")


def _http_fsm(exc: CopilotFsmError, *, correlation_id: str) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail=exc.to_detail(correlation_id or None),
    )


def _http_not_found(*, correlation_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=error_payload(
            error_code=WBP_SCENARIO_CLIENT,
            message="Session not found",
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
        "copilot_api_server_error",
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


@router.post("/sessions", response_model=CopilotSessionCreateResponse)
async def create_copilot_session(
    request: Request,
    user: AuthContext = Depends(get_current_user),
) -> CopilotSessionCreateResponse:
    """Create an in-memory Copilot session and return the INIT welcome turn."""
    cid = _correlation_id(request)
    try:
        state = sessions.create_session(advisor_id=user.advisor_id)
        return build_welcome_response(state)
    except Exception as exc:  # noqa: BLE001
        raise _http_server(
            exc, error_code=WBP_IMPORT_SERVER, correlation_id=cid
        ) from exc


@router.post(
    "/sessions/{session_id}/actions",
    response_model=CopilotAgentResponse,
)
async def post_copilot_action(
    session_id: str,
    payload: CopilotActionRequest,
    request: Request,
    user: AuthContext = Depends(get_current_user),
    rules: RulesEngine = Depends(get_rules_engine),
) -> CopilotAgentResponse:
    """Opaque action handler — advances the linear FSM."""
    cid = _correlation_id(request)
    limits = await rules.get_technical_limits()
    try:
        return await handle_action(
            session_id=session_id,
            advisor_id=user.advisor_id,
            request=payload,
            limits=limits,
        )
    except CopilotSessionError as exc:
        raise _http_not_found(correlation_id=cid) from exc
    except CopilotFsmError as exc:
        raise _http_fsm(exc, correlation_id=cid) from exc
    except Exception as exc:  # noqa: BLE001
        raise _http_server(
            exc, error_code=WBP_SCENARIO_SERVER, correlation_id=cid
        ) from exc


@router.post(
    "/sessions/{session_id}/messages",
    response_model=CopilotAgentResponse,
)
async def post_copilot_message(
    session_id: str,
    payload: CopilotMessageRequest,
    request: Request,
    user: AuthContext = Depends(get_current_user),
) -> CopilotAgentResponse:
    """Free-text handler (scenario prompt / param edits / guardrail redirect)."""
    cid = _correlation_id(request)
    try:
        return await handle_message(
            session_id=session_id,
            advisor_id=user.advisor_id,
            text=payload.text,
        )
    except CopilotSessionError as exc:
        raise _http_not_found(correlation_id=cid) from exc
    except CopilotFsmError as exc:
        raise _http_fsm(exc, correlation_id=cid) from exc
    except Exception as exc:  # noqa: BLE001
        raise _http_server(
            exc, error_code=WBP_SCENARIO_SERVER, correlation_id=cid
        ) from exc
