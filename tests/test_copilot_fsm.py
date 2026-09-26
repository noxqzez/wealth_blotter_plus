"""Unit tests for Excel Copilot FSM (linear MVP, no DB)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from backend.app.schemas.copilot import WorkflowState
from backend.app.schemas.portfolio_import import EnrichedPosition
from backend.app.schemas.scenario import (
    ScenarioBlock,
    ScenarioParams,
)
from backend.app.services import copilot_session as sessions
from backend.app.services.copilot_fsm import (
    ACTION_ENRICH,
    ACTION_PROCESS,
    ACTION_RUN,
    CopilotFsmError,
    build_scratchpad_write,
    build_welcome_response,
    handle_action,
    handle_message,
    try_deterministic_param_patch,
)
from backend.app.services.copilot_guardrails import validate_layer1
from backend.app.schemas.copilot import CopilotActionRequest


@pytest.fixture(autouse=True)
def _clean_sessions() -> None:
    sessions.clear_all_sessions()
    yield
    sessions.clear_all_sessions()


def test_welcome_init_actions() -> None:
    state = sessions.create_session(advisor_id=uuid4())
    resp = build_welcome_response(state)
    assert resp.current_state == WorkflowState.INIT
    assert [a.id for a in resp.actions] == [ACTION_PROCESS]
    assert resp.accept_user_text is False
    assert resp.excel_writes == []


@pytest.mark.asyncio
async def test_action_not_pending_rejected() -> None:
    advisor = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    with pytest.raises(CopilotFsmError) as exc:
        await handle_action(
            session_id=state.session_id,
            advisor_id=advisor,
            request=CopilotActionRequest(action_id=ACTION_ENRICH),
        )
    assert exc.value.http_status == 400


@pytest.mark.asyncio
async def test_process_requires_rows() -> None:
    advisor = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    with pytest.raises(CopilotFsmError) as exc:
        await handle_action(
            session_id=state.session_id,
            advisor_id=advisor,
            request=CopilotActionRequest(action_id=ACTION_PROCESS),
        )
    assert "rows" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_message_redirect_keeps_actions_in_init() -> None:
    advisor = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    resp = await handle_message(
        session_id=state.session_id,
        advisor_id=advisor,
        text="Tell me a joke about cats please now",
    )
    assert resp.current_state == WorkflowState.INIT
    assert any(a.id == ACTION_PROCESS for a in resp.actions)
    assert resp.accept_user_text is False


def test_layer1_rejects_empty_and_overlong() -> None:
    empty = validate_layer1("   ")
    assert empty.ok is False
    over = validate_layer1("x" * 1001)
    assert over.ok is False
    mash = validate_layer1("qwrtypzxcvb")
    assert mash.ok is False
    ok = validate_layer1("Geopolitical risk escalates, Brent reaches 120")
    assert ok.ok is True


def test_deterministic_param_patch_lookback() -> None:
    params = ScenarioParams(
        user_prompt="oil shock",
        scenario=ScenarioBlock(
            scenario_name="Oil",
            scenario_type="macro",
            lookback_window="5Y",
        ),
    )
    patched = try_deterministic_param_patch("change lookback window to 3Y", params)
    assert patched is not None
    assert patched.scenario.lookback_window == "3Y"
    assert patched.market_data_request_params.lookback_window == "3Y"


def test_scratchpad_write_has_header_and_hide() -> None:
    pos = EnrichedPosition(
        pos_id=uuid4(),
        portfolio_id=uuid4(),
        client_alias="demo",
        asset_class="EQUITY",
        ticker="AAPL",
        quantity=10,
        spot_price=100.0,
        market_value=1000.0,
        currency="USD",
        short_name="Apple",
        current_weight_pct=0.5,
        price_timestamp=datetime.now(timezone.utc),
        status="VALID",
    )
    write = build_scratchpad_write([pos], hide_source_sheet="Paste")
    assert write.sheet_name == "portfolio_scratchpad"
    assert write.hide_source_sheet == "Paste"
    assert write.values[0][0] == "client_alias"
    assert write.values[1][1] == "AAPL"
    assert "last_price" in write.column_number_formats


@pytest.mark.asyncio
async def test_message_blocked_after_confirm_pending_run_only() -> None:
    advisor = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    state.current_state = WorkflowState.SCENARIO_PARSED
    state.pending_action_ids = [ACTION_RUN]
    sessions.save_session(state)
    resp = await handle_message(
        session_id=state.session_id,
        advisor_id=advisor,
        text="change lookback to 3Y please",
    )
    assert resp.current_state == WorkflowState.SCENARIO_PARSED
    assert [a.id for a in resp.actions] == [ACTION_RUN]
    assert resp.accept_user_text is False
