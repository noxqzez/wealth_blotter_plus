"""Excel Copilot FSM API (main workflow chat; separate from compliance chat)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from backend.app.api.http_util import correlation_id, http_error, http_server_error
from backend.app.core.error_codes import (
    WBP_IMPORT_SERVER,
    WBP_SCENARIO_CLIENT,
    WBP_SCENARIO_SERVER,
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


def _http_fsm(exc: CopilotFsmError, *, cid: str) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail=exc.to_detail(cid or None),
    )


_SESSION_GONE = (
    "Chat session expired or was reset (common after reloading the "
    "task pane or restarting the backend). Reopen Copilot to start "
    "a new session, then continue from Import securities."
)


@router.post("/sessions", response_model=CopilotSessionCreateResponse)
async def create_copilot_session(
    request: Request,
    user: AuthContext = Depends(get_current_user),
) -> CopilotSessionCreateResponse:
    """Create an in-memory Copilot session and return the INIT welcome turn."""
    cid = correlation_id(request)
    try:
        state = sessions.create_session(advisor_id=user.advisor_id)
        return build_welcome_response(state)
    except Exception as exc:  # noqa: BLE001
        raise http_server_error(
            exc,
            error_code=WBP_IMPORT_SERVER,
            correlation_id=cid,
            log_event="copilot_api_server_error",
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
    cid = correlation_id(request)
    limits = await rules.get_technical_limits()
    try:
        return await handle_action(
            session_id=session_id,
            advisor_id=user.advisor_id,
            request=payload,
            limits=limits,
        )
    except CopilotSessionError as exc:
        raise http_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code=WBP_SCENARIO_CLIENT,
            message=str(exc) or _SESSION_GONE,
            correlation_id=cid,
        ) from exc
    except CopilotFsmError as exc:
        raise _http_fsm(exc, cid=cid) from exc
    except Exception as exc:  # noqa: BLE001
        raise http_server_error(
            exc,
            error_code=WBP_SCENARIO_SERVER,
            correlation_id=cid,
            log_event="copilot_api_server_error",
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
    rules: RulesEngine = Depends(get_rules_engine),
) -> CopilotAgentResponse:
    """Free-text handler (ops lookups / scenario prompt / param edits)."""
    cid = correlation_id(request)
    limits = await rules.get_technical_limits()
    try:
        return await handle_message(
            session_id=session_id,
            advisor_id=user.advisor_id,
            text=payload.text,
            limits=limits,
        )
    except CopilotSessionError as exc:
        raise http_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code=WBP_SCENARIO_CLIENT,
            message=str(exc) or _SESSION_GONE,
            correlation_id=cid,
        ) from exc
    except CopilotFsmError as exc:
        raise _http_fsm(exc, cid=cid) from exc
    except Exception as exc:  # noqa: BLE001
        raise http_server_error(
            exc,
            error_code=WBP_SCENARIO_SERVER,
            correlation_id=cid,
            log_event="copilot_api_server_error",
        ) from exc
