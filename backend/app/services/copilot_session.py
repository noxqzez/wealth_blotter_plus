"""In-memory Copilot session store (MVP: close pane = hard quit)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import structlog

from backend.app.schemas.copilot import CopilotSessionState, WorkflowState

logger = structlog.get_logger(__name__)

_lock = threading.RLock()
_sessions: dict[str, CopilotSessionState] = {}


class CopilotSessionError(LookupError):
    """Session missing or not owned by the requesting advisor."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_session(*, advisor_id: UUID) -> CopilotSessionState:
    """Create a new INIT session owned by ``advisor_id``."""
    session_id = str(uuid4())
    state = CopilotSessionState(
        session_id=session_id,
        advisor_id=advisor_id,
        current_state=WorkflowState.INIT,
        pending_action_ids=["process_security_list"],
    )
    with _lock:
        _sessions[session_id] = state
    logger.info(
        "copilot_session_created",
        session_id=session_id,
        advisor_id=str(advisor_id),
    )
    return state.model_copy(deep=True)


def get_session(*, session_id: str, advisor_id: UUID) -> CopilotSessionState:
    """Return a deep copy of the session; raise if missing or wrong owner."""
    with _lock:
        state = _sessions.get(session_id)
        if state is None or state.advisor_id != advisor_id:
            raise CopilotSessionError("Session not found")
        return state.model_copy(deep=True)


def save_session(state: CopilotSessionState) -> CopilotSessionState:
    """Persist an updated session snapshot (ownership already enforced by caller)."""
    with _lock:
        existing = _sessions.get(state.session_id)
        if existing is None or existing.advisor_id != state.advisor_id:
            raise CopilotSessionError("Session not found")
        _sessions[state.session_id] = state.model_copy(deep=True)
        return state.model_copy(deep=True)


def update_session(
    *,
    session_id: str,
    advisor_id: UUID,
    **fields: Any,
) -> CopilotSessionState:
    """Patch session fields and return the updated snapshot."""
    with _lock:
        state = _sessions.get(session_id)
        if state is None or state.advisor_id != advisor_id:
            raise CopilotSessionError("Session not found")
        updated = state.model_copy(update=fields)
        _sessions[session_id] = updated
        return updated.model_copy(deep=True)


def clear_all_sessions() -> None:
    """Test helper: wipe in-memory store."""
    with _lock:
        _sessions.clear()


def new_message_id() -> str:
    return str(uuid4())


def session_timestamp() -> str:
    return _now_iso()
