"""Pilot golden-path E2E — mocked Supabase / LLM / Resend / market I/O.

Run: ``uv run pytest -m e2e``
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.schemas.portfolio_import import (
    BuildBaseResponse,
    DraftPosition,
    EnrichedPosition,
    ValidateImportResponse,
)
from backend.app.schemas.scenario import (
    ClientPortfolioMetrics,
    OptiPosition,
    PastAnalogueArtifact,
    ScenarioBlock,
    ScenarioParams,
    ScenarioParseResponse,
    ScenarioRunResponse,
    ScenarioUpdateParamsResponse,
    StagedTrade,
    StrategyView,
)
from backend.app.services.document_upload_guards import DocumentUploadDisabledError
from tests.e2e.conftest import FakeSupabase

pytestmark = pytest.mark.e2e


def test_health(e2e_api_client: TestClient) -> None:
    res = e2e_api_client.get("/api/v1/health")
    assert res.status_code == 200
    body = res.json()
    assert body.get("status") in {"ok", "healthy", "up"} or "status" in body


def test_auth_gate_rejects_unauthenticated_portfolio(
    e2e_strict_client: TestClient,
) -> None:
    res = e2e_strict_client.post(
        "/api/v1/portfolios/validate-import",
        json={
            "rows": [
                {
                    "client_alias": "x",
                    "asset_class": "EQUITY",
                    "ticker": "AAPL",
                    "quantity": 1,
                    "currency": "USD",
                }
            ]
        },
    )
    assert res.status_code in {401, 403}


def test_register_shape_mocked(e2e_api_client: TestClient) -> None:
    from backend.app.schemas.pilot_auth import RegisterResponse

    fake = RegisterResponse(
        advisor_id=uuid4(),
        email="pilot@example.com",
        approval_status="PENDING_APPROVAL",
        message="Registered; awaiting approval.",
    )
    with patch(
        "backend.app.api.v1.auth_pilot.svc.register_pilot",
        return_value=fake,
    ):
        res = e2e_api_client.post(
            "/api/v1/auth/register",
            json={"email": "pilot@example.com", "password": "password123"},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["approval_status"] == "PENDING_APPROVAL"
    assert data["email"] == "pilot@example.com"


def test_portfolio_validate_then_build_base(
    e2e_api_client: TestClient,
    patch_supabase: FakeSupabase,
    patch_trading_rules: None,
    patch_market_quotes: None,
    silence_audit: None,
    sample_import_rows: list[dict[str, Any]],
) -> None:
    v = e2e_api_client.post(
        "/api/v1/portfolios/validate-import",
        json={"rows": sample_import_rows},
    )
    assert v.status_code == 200, v.text
    vbody = v.json()
    assert vbody["folio_stage"] == "DRAFT"
    assert vbody["client_alias"] == "e2e_client"
    assert len(vbody["positions"]) >= 2
    portfolio_id = vbody["portfolio_id"]
    draft_id = vbody["draft_id"]

    b = e2e_api_client.post(
        "/api/v1/portfolios/build-base",
        json={"portfolio_id": portfolio_id, "draft_id": draft_id},
    )
    assert b.status_code == 200, b.text
    bbody = b.json()
    assert bbody["portfolio_id"] == portfolio_id
    assert len(bbody["positions"]) >= 1
    assert any(p["ticker"] == "AAPL" for p in bbody["positions"])


def test_scenario_parse_params_run_with_past_analogue(
    e2e_api_client: TestClient,
) -> None:
    portfolio_id = uuid4()
    run_id = uuid4()
    params = ScenarioParams(
        user_prompt="Oil shock — Brent to 120",
        scenario=ScenarioBlock(
            scenario_name="OilShock",
            scenario_type="macro",
            severity="high",
            tone="risk_off",
        ),
    )
    parse_resp = ScenarioParseResponse(
        scenario_run_id=run_id,
        portfolio_id=portfolio_id,
        agent_scenario_params=params,
        scenario_params=params,
        messages=["parsed"],
    )
    update_resp = ScenarioUpdateParamsResponse(
        scenario_run_id=run_id,
        scenario_params=params,
        scenario_params_version=2,
        diffs=[],
        is_exact_match=True,
    )
    pa = PastAnalogueArtifact(
        enabled=True,
        applied=True,
        shadow=False,
        chat_summary="weight input from historical events = 0.50",
        messages=["past_analogue applied"],
        weights_shadow={"AAPL": 0.4, "MSFT": 0.3, "USD": 0.3},
    )
    run_resp = ScenarioRunResponse(
        scenario_run_id=run_id,
        portfolio_id=portfolio_id,
        status="COMPLETED",
        scenario_params=params,
        strategy_view=StrategyView(summary="stub", source="stub"),
        base_client_metrics=ClientPortfolioMetrics(expected_return=0.05),
        opti_folio=[
            OptiPosition(
                ticker="AAPL",
                asset_class="EQUITY",
                quantity=10,
                weight_pct=0.4,
            )
        ],
        opti_client_metrics=ClientPortfolioMetrics(expected_return=0.06),
        staged_trades=[
            StagedTrade(
                order_id=str(uuid4()),
                ticker="AAPL",
                asset_class="EQUITY",
                side="BUY",
                quantity=2,
            )
        ],
        past_analogue=pa,
        messages=["ok"],
    )

    with (
        patch(
            "backend.app.api.v1.scenarios.parse_and_create_run",
            AsyncMock(return_value=parse_resp),
        ),
        patch(
            "backend.app.api.v1.scenarios.update_scenario_params",
            AsyncMock(return_value=update_resp),
        ),
        patch(
            "backend.app.api.v1.scenarios.execute_scenario_run",
            AsyncMock(return_value=run_resp),
        ),
        patch(
            "backend.app.api.v1.scenarios.build_pdf_bytes",
            AsyncMock(
                return_value=(b"%PDF-1.4 e2e", "scenario.pdf", "e2e_client")
            ),
        ),
        patch(
            "backend.app.services.usage_telemetry.record_usage_metric",
            MagicMock(return_value=None),
        ),
        patch(
            "backend.app.api.v1.scenarios.record_usage_metric",
            MagicMock(return_value=None),
        ),
    ):
        p = e2e_api_client.post(
            "/api/v1/scenarios/parse",
            json={
                "portfolio_id": str(portfolio_id),
                "user_prompt": "Oil shock — Brent to 120",
            },
        )
        assert p.status_code == 200, p.text
        assert p.json()["scenario_run_id"] == str(run_id)

        u = e2e_api_client.put(
            "/api/v1/scenarios/params",
            json={
                "scenario_run_id": str(run_id),
                "scenario_params": params.model_dump(mode="json"),
            },
        )
        assert u.status_code == 200, u.text
        assert u.json()["is_exact_match"] is True

        r = e2e_api_client.post(
            "/api/v1/scenarios/run",
            json={"scenario_run_id": str(run_id)},
        )
        assert r.status_code == 200, r.text
        rbody = r.json()
        assert rbody["status"] == "COMPLETED"
        assert rbody["past_analogue"] is not None
        assert rbody["past_analogue"]["enabled"] is True
        assert rbody["staged_trades"]
        assert rbody["opti_folio"]

        pdf = e2e_api_client.post(
            "/api/v1/scenarios/pdf",
            json={"scenario_run_id": str(run_id)},
        )
        assert pdf.status_code == 200, pdf.text
        assert pdf.headers.get("content-type", "").startswith("application/pdf")
        assert pdf.content.startswith(b"%PDF")


def test_copilot_session_import_optimize_place(
    e2e_api_client: TestClient,
    sample_import_rows: list[dict[str, Any]],
) -> None:
    """HTTP Copilot FSM: session → process import → enrich → scenario stubs → place."""
    from backend.app.services.copilot_fsm import (
        ACTION_ENRICH,
        ACTION_PLACE,
        ACTION_PROCESS,
        ACTION_RUN,
    )

    portfolio_id = uuid4()
    draft_id = uuid4()
    now = datetime.now(timezone.utc)

    validate = ValidateImportResponse(
        portfolio_id=portfolio_id,
        draft_id=draft_id,
        client_alias="e2e_client",
        positions=[
            DraftPosition(
                client_alias="e2e_client",
                asset_class="EQUITY",
                ticker="AAPL",
                quantity=10,
                currency="USD",
            ),
            DraftPosition(
                client_alias="e2e_client",
                asset_class="CASH",
                ticker="USD",
                quantity=1000,
                currency="USD",
            ),
        ],
        messages=[],
    )
    build = BuildBaseResponse(
        portfolio_id=portfolio_id,
        draft_id=draft_id,
        client_alias="e2e_client",
        positions=[
            EnrichedPosition(
                pos_id=uuid4(),
                portfolio_id=portfolio_id,
                client_alias="e2e_client",
                asset_class="EQUITY",
                ticker="AAPL",
                quantity=10,
                spot_price=100.0,
                market_value=1000.0,
                currency="USD",
                short_name="Apple",
                current_weight_pct=0.5,
                price_timestamp=now,
                status="VALID",
            ),
            EnrichedPosition(
                pos_id=uuid4(),
                portfolio_id=portfolio_id,
                client_alias="e2e_client",
                asset_class="CASH",
                ticker="USD",
                quantity=1000,
                spot_price=1.0,
                market_value=1000.0,
                currency="USD",
                short_name="USD",
                current_weight_pct=0.5,
                price_timestamp=now,
                status="VALID",
            ),
        ],
        warnings=[],
    )

    run_id = uuid4()
    params = ScenarioParams(
        user_prompt="Mild equity rally",
        scenario=ScenarioBlock(
            scenario_name="Rally",
            scenario_type="equity_rally",
            tone="risk_on",
        ),
    )

    async def _fake_parse(**kwargs: Any) -> ScenarioParseResponse:
        return ScenarioParseResponse(
            scenario_run_id=run_id,
            portfolio_id=portfolio_id,
            agent_scenario_params=params,
            scenario_params=params,
        )

    async def _fake_execute(**kwargs: Any) -> ScenarioRunResponse:
        return ScenarioRunResponse(
            scenario_run_id=run_id,
            portfolio_id=portfolio_id,
            status="COMPLETED",
            scenario_params=params,
            strategy_view=StrategyView(summary="ok", source="stub"),
            opti_folio=[
                OptiPosition(
                    ticker="AAPL",
                    asset_class="EQUITY",
                    quantity=12,
                    weight_pct=0.6,
                )
            ],
            staged_trades=[
                StagedTrade(
                    order_id=str(uuid4()),
                    ticker="AAPL",
                    asset_class="EQUITY",
                    side="BUY",
                    quantity=2,
                )
            ],
            past_analogue=PastAnalogueArtifact(
                enabled=True,
                applied=False,
                shadow=True,
                chat_summary="historical weight input = 0.00 (shadow)",
            ),
        )

    def _fake_place_sync(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "orders": [
                StagedTrade(
                    order_id="1",
                    status="Executed",
                    ticker="AAPL",
                    asset_class="EQUITY",
                    side="BUY",
                    quantity=2,
                )
            ],
            "orders_hitl": False,
            "mismatch_notes": [],
        }

    with (
        patch(
            "backend.app.services.copilot_fsm.validate_import",
            AsyncMock(return_value=validate),
        ),
        patch(
            "backend.app.services.copilot_fsm.build_base",
            AsyncMock(return_value=build),
        ),
        patch(
            "backend.app.services.copilot_fsm.set_portfolio_import_hitl",
            MagicMock(),
        ),
        patch(
            "backend.app.services.copilot_fsm.parse_and_create_run",
            _fake_parse,
        ),
        patch(
            "backend.app.services.copilot_fsm.execute_scenario_run",
            _fake_execute,
        ),
        patch(
            "backend.app.services.copilot_fsm.place_all_staged_orders",
            _fake_place_sync,
        ),
        patch(
            "backend.app.services.copilot_fsm.has_rating_for_run",
            return_value=True,
        ),
    ):
        s = e2e_api_client.post("/api/v1/copilot/sessions")
        assert s.status_code == 200, s.text
        session_id = s.json()["session_id"]
        assert s.json()["current_state"] == "INIT"
        assert any(a["id"] == ACTION_PROCESS for a in s.json()["actions"])

        proc = e2e_api_client.post(
            f"/api/v1/copilot/sessions/{session_id}/actions",
            json={"action_id": ACTION_PROCESS, "rows": sample_import_rows},
        )
        assert proc.status_code == 200, proc.text
        state = proc.json()["current_state"]
        assert state in {
            "BLOTTER_VALIDATED",
            "BLOTTER_HAS_ERRORS",
            "PORTFOLIO_ENRICHED",
        }

        if state == "BLOTTER_VALIDATED":
            enr = e2e_api_client.post(
                f"/api/v1/copilot/sessions/{session_id}/actions",
                json={"action_id": ACTION_ENRICH},
            )
            assert enr.status_code == 200, enr.text
            state = enr.json()["current_state"]

        if state == "BLOTTER_HAS_ERRORS":
            pytest.fail(f"Import errors: {proc.json().get('content')}")

        assert state == "PORTFOLIO_ENRICHED"

        msg = e2e_api_client.post(
            f"/api/v1/copilot/sessions/{session_id}/messages",
            json={"text": "Mild equity rally over the next quarter"},
        )
        assert msg.status_code == 200, msg.text
        assert msg.json()["current_state"] == "SCENARIO_PARSED"

        actions = {a["id"] for a in msg.json()["actions"]}
        if "confirm_scenario_params" in actions:
            conf = e2e_api_client.post(
                f"/api/v1/copilot/sessions/{session_id}/actions",
                json={"action_id": "confirm_scenario_params"},
            )
            assert conf.status_code == 200, conf.text
            state = conf.json()["current_state"]
        elif ACTION_RUN in actions:
            run = e2e_api_client.post(
                f"/api/v1/copilot/sessions/{session_id}/actions",
                json={"action_id": ACTION_RUN},
            )
            assert run.status_code == 200, run.text
            state = run.json()["current_state"]
        else:
            pytest.fail(f"Unexpected actions after parse: {actions}")

        assert state == "OPTIMIZATION_COMPLETE"

        place = e2e_api_client.post(
            f"/api/v1/copilot/sessions/{session_id}/actions",
            json={
                "action_id": ACTION_PLACE,
                "order_rows": [
                    {
                        "order_id": str(uuid4()),
                        "ticker": "AAPL",
                        "side": "BUY",
                        "quantity": 2,
                        "status": "Pending",
                        "asset_class": "EQUITY",
                    }
                ],
            },
        )
        assert place.status_code == 200, place.text
        assert place.json()["current_state"] == "ORDERS_PLACED"


def test_rag_upload_kill_switch_api(e2e_api_client: TestClient) -> None:
    with patch(
        "backend.app.api.v1.documents.upload_research_pdf",
        AsyncMock(
            side_effect=DocumentUploadDisabledError("RAG uploads disabled")
        ),
    ):
        res = e2e_api_client.post(
            "/api/v1/documents/upload",
            files={"file": ("note.pdf", b"%PDF-1.4", "application/pdf")},
        )
    assert res.status_code == 403
    assert "disabled" in res.json()["detail"].lower()


def test_admin_welcome_requires_key_and_succeeds(
    e2e_api_client: TestClient,
) -> None:
    from backend.app.schemas.pilot_admin import ApproveAndWelcomeResponse

    aid = uuid4()
    fake = ApproveAndWelcomeResponse(
        advisor_id=aid,
        email="pilot@example.com",
        approval_status="APPROVED",
        email_sent=True,
        email_skipped=False,
        skip_reason=None,
        message="ok",
    )
    with (
        patch("backend.app.api.v1.admin_pilots.get_settings") as gs,
        patch(
            "backend.app.api.v1.admin_pilots.admin_svc.approve_and_welcome",
            return_value=fake,
        ),
    ):
        settings = MagicMock()
        settings.admin_api_key = "e2e-admin-key"
        settings.environment = "development"
        gs.return_value = settings

        bad = e2e_api_client.post(
            "/api/v1/admin/pilots/approve-and-welcome",
            json={"email": "pilot@example.com"},
        )
        assert bad.status_code == 401

        ok = e2e_api_client.post(
            "/api/v1/admin/pilots/approve-and-welcome",
            json={"email": "pilot@example.com"},
            headers={"X-Admin-Key": "e2e-admin-key"},
        )
        assert ok.status_code == 200
        assert ok.json()["email_sent"] is True
        assert ok.json()["approval_status"] == "APPROVED"


def test_byok_required_in_production_context() -> None:
    """Security gate: production + REQUIRE_BYOK rejects missing credentials."""
    from fastapi import HTTPException

    from backend.app.core.byok import clear_byok, require_byok_for_llm

    clear_byok()
    settings = MagicMock()
    settings.environment = "production"
    settings.require_byok = True
    with pytest.raises(HTTPException) as exc:
        require_byok_for_llm(settings=settings)
    assert exc.value.status_code == 400
    clear_byok()
