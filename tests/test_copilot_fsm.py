"""Unit tests for Excel Copilot FSM (linear MVP, no DB)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from backend.app.schemas.copilot import IntentClass, WorkflowState
from backend.app.schemas.portfolio_import import EnrichedPosition
from backend.app.schemas.scenario import (
    ScenarioBlock,
    ScenarioParams,
)
from backend.app.services import copilot_session as sessions
from backend.app.services.copilot_common import (
    _orders_placed_action_ids,
    _post_opti_action_ids,
)
from backend.app.services.copilot_fsm import (
    ACTION_CONFIRM,
    ACTION_ENRICH,
    ACTION_NEW_SCENARIO,
    ACTION_PLACE,
    ACTION_PDF,
    ACTION_PROCESS,
    ACTION_REUSE_LAST_SCENARIO,
    ACTION_RUN,
    ACTION_SKIP_RATING,
    ACTION_UPDATE_HOLDINGS,
    CopilotFsmError,
    build_scratchpad_write,
    build_welcome_response,
    handle_action,
    handle_message,
    try_deterministic_param_patch,
)
from backend.app.services.copilot_excel_writes import (
    ORD_BLOTTER_SHEET,
    REBALANCED_SHEET,
)
from backend.app.services.copilot_guardrails import (
    classify_identifier_intent,
    validate_layer1,
)
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
    assert resp.status_label == "Import securities"
    assert [a.id for a in resp.actions] == [ACTION_PROCESS]
    assert resp.accept_user_text is True
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
    assert resp.accept_user_text is True


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


def test_deterministic_param_patch_tone_from_to() -> None:
    params = ScenarioParams(
        user_prompt="rally",
        scenario=ScenarioBlock(
            scenario_name="Rally",
            scenario_type="equity_rally",
            tone="risk_off",
        ),
    )
    patched = try_deterministic_param_patch("change risk_off to risk_on", params)
    assert patched is not None
    assert patched.scenario.tone == "risk_on"
    patched2 = try_deterministic_param_patch("tone to risk_on", params)
    assert patched2 is not None
    assert patched2.scenario.tone == "risk_on"


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
    assert write.header_bold is True
    assert write.table_border is True
    assert write.header_fill_colors.get("client_alias") == "D9D9D9"
    assert write.header_fill_colors.get("short_name") == "BDD7EE"
    assert write.header_horizontal_alignment == "center"


@pytest.mark.asyncio
async def test_confirm_auto_triggers_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advisor = uuid4()
    run_id = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    state.current_state = WorkflowState.SCENARIO_PARSED
    state.scenario_run_id = run_id
    state.scenario_params = ScenarioParams(
        user_prompt="rally",
        scenario=ScenarioBlock(
            scenario_name="Rally",
            scenario_type="equity_rally",
            tone="risk_on",
        ),
    ).model_dump(mode="json")
    state.params_dirty = False
    state.pending_action_ids = [ACTION_CONFIRM]
    sessions.save_session(state)

    async def _fake_run(st, *, advisor_id, limits=None):  # noqa: ANN001
        st.current_state = WorkflowState.OPTIMIZATION_COMPLETE
        st.pending_action_ids = [ACTION_PLACE, ACTION_PDF]
        sessions.save_session(st)
        from backend.app.services.copilot_fsm import _response

        return _response(
            st,
            content="Optimization complete. Wrote sheets.",
            actions=[],
            badges=["OPTIMIZATION_COMPLETE"],
        )

    monkeypatch.setattr(
        "backend.app.services.copilot_actions_workflow._handle_run",
        _fake_run,
    )
    resp = await handle_action(
        session_id=state.session_id,
        advisor_id=advisor,
        request=CopilotActionRequest(action_id=ACTION_CONFIRM),
    )
    assert "confirmed" in resp.content.lower()
    assert "optimization complete" in resp.content.lower()
    assert resp.current_state == WorkflowState.OPTIMIZATION_COMPLETE


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
    assert resp.accept_user_text is True


def test_identifier_write_vs_recall_classification() -> None:
    pid = str(uuid4())
    write_intent, write_refs = classify_identifier_intent(
        f"set portfolioID to {pid}"
    )
    assert write_intent == IntentClass.ID_WRITE_ATTEMPT
    assert write_refs and write_refs[0].kind == "portfolio_id"

    recall_intent, recall_refs = classify_identifier_intent(
        f"show portfolioID {pid}"
    )
    assert recall_intent == IntentClass.ID_RECALL
    assert recall_refs and recall_refs[0].value == pid

    order_intent, order_refs = classify_identifier_intent(
        "recall orderID ST-ABCDEF12-AAPL-BUY-001"
    )
    assert order_intent == IntentClass.ID_RECALL
    assert order_refs and order_refs[0].kind == "order_id"


@pytest.mark.asyncio
async def test_message_blocks_identifier_write_without_mutating_session() -> None:
    advisor = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    original_portfolio = uuid4()
    state.portfolio_id = original_portfolio
    state.current_state = WorkflowState.PORTFOLIO_ENRICHED
    state.pending_action_ids = []
    sessions.save_session(state)

    injected = uuid4()
    resp = await handle_message(
        session_id=state.session_id,
        advisor_id=advisor,
        text=f"use portfolio_id {injected} for this run",
    )
    refreshed = sessions.get_session(
        session_id=state.session_id, advisor_id=advisor
    )
    assert refreshed.portfolio_id == original_portfolio
    assert resp.portfolio_id == original_portfolio
    assert any(g.rule_id == "copilot.id_write_blocked" for g in resp.guardrails)
    assert "read-only" in resp.content.lower()


@pytest.mark.asyncio
async def test_message_id_recall_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advisor = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    original_run = uuid4()
    state.scenario_run_id = original_run
    state.current_state = WorkflowState.INIT
    sessions.save_session(state)

    looked_up = uuid4()

    def _fake_recall(refs, *, advisor_id):  # noqa: ANN001
        assert advisor_id == advisor
        assert refs
        return f"READ_ONLY_OK `{looked_up}`"

    monkeypatch.setattr(
        "backend.app.services.copilot_fsm.format_id_recall",
        _fake_recall,
    )
    resp = await handle_message(
        session_id=state.session_id,
        advisor_id=advisor,
        text=f"audit scenarioID {looked_up}",
    )
    refreshed = sessions.get_session(
        session_id=state.session_id, advisor_id=advisor
    )
    assert refreshed.scenario_run_id == original_run
    assert resp.scenario_run_id == original_run
    assert "READ_ONLY_OK" in resp.content
    assert any(g.rule_id == "copilot.id_recall" for g in resp.guardrails)


@pytest.mark.asyncio
async def test_rating_saved_once_per_simulation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advisor = uuid4()
    run_id = uuid4()
    portfolio_id = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    state.current_state = WorkflowState.OPTIMIZATION_COMPLETE
    state.scenario_run_id = run_id
    state.portfolio_id = portfolio_id
    state.rating_for_run_id = run_id
    state.rating_captured = False
    state.pending_action_ids = [
        "rate_stars_1",
        "rate_stars_2",
        "rate_stars_3",
        "rate_stars_4",
        "rate_stars_5",
        ACTION_SKIP_RATING,
        ACTION_PLACE,
        ACTION_PDF,
    ]
    sessions.save_session(state)

    monkeypatch.setattr(
        "backend.app.services.copilot_common.has_rating_for_run",
        lambda **_kwargs: False,
    )
    saved: dict[str, object] = {}

    def _fake_save(**kwargs):  # noqa: ANN003
        saved.update(kwargs)
        return {"rating": kwargs["rating"]}

    monkeypatch.setattr(
        "backend.app.services.copilot_actions_workflow.save_interaction_rating",
        _fake_save,
    )

    resp = await handle_action(
        session_id=state.session_id,
        advisor_id=advisor,
        request=CopilotActionRequest(action_id="rate_stars_4"),
    )
    assert saved["rating"] == 4
    assert saved["advisor_id"] == advisor
    assert saved["scenario_run_id"] == run_id
    assert "4/5" in resp.content
    assert ACTION_PLACE in [a.id for a in resp.actions]
    assert not any(a.id.startswith("rate_stars_") for a in resp.actions)
    assert resp.show_star_rating is False

    refreshed = sessions.get_session(
        session_id=state.session_id, advisor_id=advisor
    )
    assert refreshed.rating_captured is True

    # Second attempt rejected by pending-action gate (stars no longer pending).
    with pytest.raises(CopilotFsmError):
        await handle_action(
            session_id=state.session_id,
            advisor_id=advisor,
            request=CopilotActionRequest(action_id="rate_stars_5"),
        )


@pytest.mark.asyncio
async def test_rating_skip_does_not_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advisor = uuid4()
    run_id = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    state.current_state = WorkflowState.OPTIMIZATION_COMPLETE
    state.scenario_run_id = run_id
    state.rating_for_run_id = run_id
    state.rating_captured = False
    state.pending_action_ids = [
        "rate_stars_1",
        ACTION_SKIP_RATING,
        ACTION_PLACE,
        ACTION_PDF,
    ]
    sessions.save_session(state)

    monkeypatch.setattr(
        "backend.app.services.copilot_common.has_rating_for_run",
        lambda **_kwargs: False,
    )
    called = {"save": False}

    def _boom(**_kwargs):  # noqa: ANN003
        called["save"] = True
        raise AssertionError("skip must not persist a rating")

    monkeypatch.setattr(
        "backend.app.services.copilot_actions_workflow.save_interaction_rating",
        _boom,
    )

    resp = await handle_action(
        session_id=state.session_id,
        advisor_id=advisor,
        request=CopilotActionRequest(action_id=ACTION_SKIP_RATING),
    )
    assert called["save"] is False
    assert "skipped" in resp.content.lower()
    assert [a.id for a in resp.actions] == [
        ACTION_PLACE,
        ACTION_PDF,
        ACTION_NEW_SCENARIO,
        ACTION_UPDATE_HOLDINGS,
    ]


@pytest.mark.asyncio
async def test_run_new_scenario_clears_draft_keeps_portfolio() -> None:
    advisor = uuid4()
    portfolio_id = uuid4()
    run_id = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    state.current_state = WorkflowState.OPTIMIZATION_COMPLETE
    state.portfolio_id = portfolio_id
    state.scenario_run_id = run_id
    state.scenario_params = {"user_prompt": "old"}
    state.params_dirty = True
    state.rating_captured = True
    state.rating_for_run_id = run_id
    state.pending_action_ids = [
        ACTION_PLACE,
        ACTION_PDF,
        ACTION_NEW_SCENARIO,
        ACTION_UPDATE_HOLDINGS,
    ]
    sessions.save_session(state)

    resp = await handle_action(
        session_id=state.session_id,
        advisor_id=advisor,
        request=CopilotActionRequest(action_id=ACTION_NEW_SCENARIO),
    )
    assert resp.current_state == WorkflowState.PORTFOLIO_ENRICHED
    assert resp.status_label == "Enter scenario"
    assert resp.accept_user_text is True
    assert resp.actions == []
    assert "new scenario" in resp.content.lower()

    refreshed = sessions.get_session(
        session_id=state.session_id, advisor_id=advisor
    )
    assert refreshed.portfolio_id == portfolio_id
    assert refreshed.scenario_run_id is None
    assert refreshed.scenario_params is None
    assert refreshed.params_dirty is False
    assert refreshed.rating_captured is False
    assert refreshed.pending_action_ids == []


@pytest.mark.asyncio
async def test_run_new_scenario_from_orders_placed() -> None:
    advisor = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    state.current_state = WorkflowState.ORDERS_PLACED
    state.portfolio_id = uuid4()
    state.scenario_run_id = uuid4()
    state.pending_action_ids = [
        ACTION_PDF,
        ACTION_NEW_SCENARIO,
        ACTION_UPDATE_HOLDINGS,
    ]
    sessions.save_session(state)

    resp = await handle_action(
        session_id=state.session_id,
        advisor_id=advisor,
        request=CopilotActionRequest(action_id=ACTION_NEW_SCENARIO),
    )
    assert resp.current_state == WorkflowState.PORTFOLIO_ENRICHED
    assert resp.scenario_run_id is None


def test_post_opti_pending_includes_update_holdings() -> None:
    state = sessions.create_session(advisor_id=uuid4())
    state.current_state = WorkflowState.OPTIMIZATION_COMPLETE
    state.scenario_run_id = uuid4()
    state.rating_captured = True
    state.rating_for_run_id = state.scenario_run_id
    ids = _post_opti_action_ids(state)
    assert ACTION_UPDATE_HOLDINGS in ids
    assert ACTION_PLACE in ids


def test_orders_placed_pending_includes_update_holdings() -> None:
    assert ACTION_UPDATE_HOLDINGS in _orders_placed_action_ids()
    assert ACTION_PROCESS in _orders_placed_action_ids()


@pytest.mark.asyncio
async def test_update_holdings_from_optimization_complete() -> None:
    advisor = uuid4()
    portfolio_id = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    state.current_state = WorkflowState.OPTIMIZATION_COMPLETE
    state.portfolio_id = portfolio_id
    state.scenario_run_id = uuid4()
    state.scenario_params = {"user_prompt": "Chinese stocks rally"}
    state.rating_captured = True
    state.rating_for_run_id = state.scenario_run_id
    state.pending_action_ids = _post_opti_action_ids(state)
    sessions.save_session(state)

    resp = await handle_action(
        session_id=state.session_id,
        advisor_id=advisor,
        request=CopilotActionRequest(action_id=ACTION_UPDATE_HOLDINGS),
    )
    assert resp.current_state == WorkflowState.INIT
    assert [a.id for a in resp.actions] == [ACTION_PROCESS]
    assert "discarded" in resp.content.lower()
    sheet_names = {w.sheet_name for w in resp.excel_writes}
    assert REBALANCED_SHEET in sheet_names
    assert ORD_BLOTTER_SHEET in sheet_names
    refreshed = sessions.get_session(
        session_id=state.session_id, advisor_id=advisor
    )
    assert refreshed.portfolio_id == portfolio_id
    assert refreshed.scenario_run_id is None
    assert refreshed.scenario_params is None
    assert refreshed.last_scenario_user_prompt == "Chinese stocks rally"


@pytest.mark.asyncio
async def test_update_holdings_via_enrich_portfolio_text() -> None:
    advisor = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    state.current_state = WorkflowState.ORDERS_PLACED
    state.portfolio_id = uuid4()
    state.pending_action_ids = _orders_placed_action_ids()
    sessions.save_session(state)

    resp = await handle_message(
        session_id=state.session_id,
        advisor_id=advisor,
        text="enrich the portfolio selected",
    )
    assert resp.current_state == WorkflowState.INIT
    assert ACTION_PROCESS in [a.id for a in resp.actions]
    assert "update holdings" in resp.content.lower()


@pytest.mark.asyncio
async def test_update_holdings_returns_to_import() -> None:
    advisor = uuid4()
    portfolio_id = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    state.current_state = WorkflowState.SCENARIO_PARSED
    state.portfolio_id = portfolio_id
    state.scenario_run_id = uuid4()
    state.scenario_params = {"user_prompt": "rally"}
    state.pending_action_ids = [ACTION_CONFIRM, ACTION_UPDATE_HOLDINGS]
    sessions.save_session(state)

    resp = await handle_action(
        session_id=state.session_id,
        advisor_id=advisor,
        request=CopilotActionRequest(action_id=ACTION_UPDATE_HOLDINGS),
    )
    assert resp.current_state == WorkflowState.INIT
    assert resp.status_label == "Import securities"
    assert [a.id for a in resp.actions] == [ACTION_PROCESS]
    assert "discarded" in resp.content.lower()
    assert "workflow restart" in resp.content.lower()

    refreshed = sessions.get_session(
        session_id=state.session_id, advisor_id=advisor
    )
    assert refreshed.portfolio_id == portfolio_id
    assert refreshed.scenario_run_id is None
    assert refreshed.scenario_params is None
    assert refreshed.last_scenario_user_prompt == "rally"
    assert refreshed.pending_action_ids == [ACTION_PROCESS]


@pytest.mark.asyncio
async def test_reuse_last_scenario_after_enrich(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advisor = uuid4()
    portfolio_id = uuid4()
    new_run_id = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    state.current_state = WorkflowState.PORTFOLIO_ENRICHED
    state.portfolio_id = portfolio_id
    state.last_scenario_user_prompt = "Chinese stocks rally"
    state.pending_action_ids = [ACTION_REUSE_LAST_SCENARIO]
    sessions.save_session(state)

    class _Parsed:
        scenario_run_id = new_run_id
        scenario_params = ScenarioParams(
            user_prompt="Chinese stocks rally",
            scenario=ScenarioBlock(
                scenario_name="China rally",
                scenario_type="macro",
            ),
        )

    async def _fake_parse(**kwargs: object) -> _Parsed:
        assert kwargs["user_prompt"] == "Chinese stocks rally"
        assert kwargs["portfolio_id"] == portfolio_id
        return _Parsed()

    monkeypatch.setattr(
        "backend.app.services.copilot_actions_workflow.parse_and_create_run",
        _fake_parse,
    )

    resp = await handle_action(
        session_id=state.session_id,
        advisor_id=advisor,
        request=CopilotActionRequest(action_id=ACTION_REUSE_LAST_SCENARIO),
    )
    assert resp.current_state == WorkflowState.SCENARIO_PARSED
    assert resp.scenario_run_id == new_run_id
    assert "REUSED_LAST_SCENARIO" in resp.execution_status_badges
    refreshed = sessions.get_session(
        session_id=state.session_id, advisor_id=advisor
    )
    assert refreshed.last_scenario_user_prompt is None
    assert refreshed.scenario_params is not None
    assert refreshed.scenario_params["user_prompt"] == "Chinese stocks rally"


@pytest.mark.asyncio
async def test_orders_placed_process_clears_scenario_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advisor = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    state.current_state = WorkflowState.ORDERS_PLACED
    state.portfolio_id = uuid4()
    state.scenario_run_id = uuid4()
    state.scenario_params = {"user_prompt": "old prompt"}
    state.pending_action_ids = _orders_placed_action_ids()
    sessions.save_session(state)

    async def _fake_validate(*_a: object, **_k: object):
        refreshed = sessions.get_session(
            session_id=state.session_id, advisor_id=advisor
        )
        assert refreshed.scenario_run_id is None
        assert refreshed.scenario_params is None
        assert refreshed.last_scenario_user_prompt == "old prompt"
        from backend.app.schemas.copilot import CopilotAgentResponse

        return CopilotAgentResponse(
            message_id="m1",
            session_id=state.session_id,
            current_state=WorkflowState.BLOTTER_VALIDATED,
            status_label="Blotter validated",
            content="ok",
        )

    monkeypatch.setattr(
        "backend.app.services.copilot_actions._validate_rows",
        _fake_validate,
    )

    await handle_action(
        session_id=state.session_id,
        advisor_id=advisor,
        request=CopilotActionRequest(
            action_id=ACTION_PROCESS,
            rows=[{"ticker": "AAPL", "quantity": 1, "asset_class": "EQUITY"}],
        ),
    )

@pytest.mark.asyncio
async def test_ops_latest_trades_does_not_nameerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: ops reply used workflow_status_label without importing it."""
    from backend.app.services.copilot_ops_models import OpsQueryResult

    advisor = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    state.current_state = WorkflowState.ORDERS_PLACED
    state.pending_action_ids = [ACTION_UPDATE_HOLDINGS, ACTION_PROCESS]
    sessions.save_session(state)

    async def _fake_ops(*_a: object, **_k: object) -> OpsQueryResult:
        return OpsQueryResult(
            ok=True,
            content="Latest trades for `e2e_demo`:\n- AAPL BUY 10",
            ops_kind="latest_trades",
            ops_client_alias="e2e_demo",
            ops_load_target="none",
        )

    monkeypatch.setattr(
        "backend.app.services.copilot_fsm.execute_ops_query",
        _fake_ops,
    )
    resp = await handle_message(
        session_id=state.session_id,
        advisor_id=advisor,
        text="what are the latest trades for e2e_demo client?",
    )
    assert "e2e_demo" in resp.content
    assert "OPS_QUERY" in resp.execution_status_badges
    assert resp.status_label  # must not raise NameError building response


@pytest.mark.asyncio
async def test_download_pdf_returns_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: PDF action must build DownloadArtifact (not NameError)."""
    advisor = uuid4()
    run_id = uuid4()
    state = sessions.create_session(advisor_id=advisor)
    state.current_state = WorkflowState.OPTIMIZATION_COMPLETE
    state.scenario_run_id = run_id
    state.rating_captured = True
    state.pending_action_ids = [ACTION_PLACE, ACTION_PDF]
    sessions.save_session(state)

    async def _fake_pdf(*, scenario_run_id, advisor_id):  # noqa: ANN001
        assert scenario_run_id == run_id
        assert advisor_id == advisor
        return b"%PDF-1.4 test", "scenario_test.pdf", "demo_client"

    monkeypatch.setattr(
        "backend.app.services.copilot_actions_workflow.build_pdf_bytes",
        _fake_pdf,
    )
    resp = await handle_action(
        session_id=state.session_id,
        advisor_id=advisor,
        request=CopilotActionRequest(action_id=ACTION_PDF),
    )
    assert resp.download is not None
    assert resp.download.kind == "pdf"
    assert resp.download.filename == "scenario_test.pdf"
    assert "PDF_READY" in resp.execution_status_badges
