"""WF-02 Phase-1 scenario orchestration (parse / params / run / PDF helpers).

HTTP layer lives in ``api/v1/scenarios.py``. Timeouts (caller-enforced):
parse ~30s, params ~15s, run ~120s, PDF ~30s (see configs.scenario_wf02).
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import structlog

from backend.app.agents.scenario_crew import parse_scenario_prompt
from backend.app.core.error_codes import (
    WBP_SCENARIO_CLIENT,
    WBP_SCENARIO_COMPLIANCE,
    WBP_SCENARIO_INVALID_PROMPT,
    WBP_SCENARIO_PARAMS,
    WBP_SCENARIO_PARSE,
    WBP_SCENARIO_PDF,
    WBP_SCENARIO_RUN,
    WBP_SCENARIO_SERVER,
    WBP_SCENARIO_TIMEOUT,
)
from backend.app.schemas.rules import TechnicalLimits
from backend.app.schemas.scenario import (
    ClientPortfolioMetrics,
    ScenarioBlock,
    ScenarioParams,
    ScenarioParseResponse,
    ScenarioRunResponse,
    ScenarioUpdateParamsResponse,
)
from backend.app.services.compliance_cite import load_active_trading_rules
from backend.app.services.scenario_audit import (
    STEP_PARAMS_UPDATE,
    STEP_PARSE,
    STEP_RUN_FAILED,
    write_scenario_step_audit,
)
from backend.app.services.scenario_db import (
    PARSE_TIMEOUT_SECONDS,
    PARAMS_TIMEOUT_SECONDS,
    PDF_TIMEOUT_SECONDS,
    RUN_TIMEOUT_SECONDS,
    ScenarioServiceError,
    _MIGRATION_HINT,
    _client,
    _guard_db,
    _is_missing_relation,
    _load_base_positions,
    _load_client_alias,
    _load_config_row,
    _load_portfolio_for_scenario,
    _load_scenario_run,
    _now_iso,
    _params_from_row,
)
from backend.app.services.scenario_execute import _execute_phase1_sync
from backend.app.services.scenario_history import (
    ScenarioHistoryError,
    build_scenario_universe,
)
from backend.app.services.scenario_optimizer import (
    ComplianceUnresolvedError,
    ScenarioOptimizerError,
)
from backend.app.services.scenario_param_diffs import diff_scenario_params
from backend.app.services.scenario_pdf import build_scenario_pdf
from backend.app.services.scenario_place import place_all_staged_orders
from backend.app.services.scenario_prompt_guard import (
    INVALID_PROMPT_MESSAGE,
    assert_scenario_prompt_in_scope,
)
from backend.app.services.scenario_research import (
    run_house_research,
    run_market_research,
)
from backend.app.services.scenario_talking_points import format_news_footnote_lines
from backend.app.services.scenario_trades import _build_staged_trades
from backend.app.services.trading_rules_policy import approved_crypto_bases_from_rules

logger = structlog.get_logger(__name__)


async def parse_and_create_run(
    *,
    portfolio_id: UUID,
    user_prompt: str,
    advisor_id: UUID,
) -> ScenarioParseResponse:
    """Create scenario_runs row (PARSED) after NL → ScenarioParams."""
    portfolio = _load_portfolio_for_scenario(portfolio_id)
    scenario_cfg = _load_config_row("scenario_wf02")
    phase = str(scenario_cfg.get("phase") or "phase1")
    if phase not in {"phase1", "phase2"}:
        phase = "phase1"
    feature_flags = scenario_cfg.get("feature_flags") or {}
    if not isinstance(feature_flags, dict):
        feature_flags = {}

    try:
        assert_scenario_prompt_in_scope(user_prompt)
    except ValueError as exc:
        raise ScenarioServiceError(
            str(exc) or INVALID_PROMPT_MESSAGE,
            error_code=WBP_SCENARIO_INVALID_PROMPT,
            http_status=400,
        ) from exc

    try:
        params = await parse_scenario_prompt(user_prompt)
    except Exception as exc:  # noqa: BLE001
        logger.exception("scenario_parse_failed", error=str(exc))
        raise ScenarioServiceError(
            f"Scenario parse failed: {exc}",
            error_code=WBP_SCENARIO_PARSE,
            http_status=400,
        ) from exc

    run_id = uuid4()
    params = params.model_copy(update={"workflow_id": str(run_id)})
    payload = {
        "id": str(run_id),
        "advisor_id": str(advisor_id),
        "portfolio_id": str(portfolio_id),
        "client_id": portfolio.get("client_id"),
        "status": "PARSED",
        "phase": phase,
        "user_prompt": user_prompt.strip(),
        "agent_scenario_params": params.model_dump(mode="json"),
        "scenario_params": params.model_dump(mode="json"),
        "scenario_params_version": 1,
        "feature_flags": feature_flags,
        "updated_at": _now_iso(),
    }
    try:
        _client().table("scenario_runs").insert(payload).execute()
    except Exception as exc:  # noqa: BLE001
        _guard_db(exc)
        raise ScenarioServiceError(
            f"Failed to persist scenario_run: {exc}",
            error_code=WBP_SCENARIO_SERVER,
        ) from exc

    client_alias = _load_client_alias(
        str(portfolio["client_id"]) if portfolio.get("client_id") else None
    )
    write_scenario_step_audit(
        scenario_run_id=run_id,
        portfolio_id=portfolio_id,
        step=STEP_PARSE,
        agent_or_component="scenario_agent",
        advisor_id=advisor_id,
        client_alias=client_alias,
        phase=phase,
        implementation=str(params.source_agent or "scenario_agent"),
        input_payload={"user_prompt": user_prompt.strip()},
        output_payload={"scenario_params": params.model_dump(mode="json")},
        messages=["Scenario parameters parsed; review or edit before run."],
    )

    logger.info(
        "scenario_parsed",
        scenario_run_id=str(run_id),
        portfolio_id=str(portfolio_id),
        scenario_type=params.scenario.scenario_type,
    )
    return ScenarioParseResponse(
        scenario_run_id=run_id,
        portfolio_id=portfolio_id,
        status="PARSED",
        phase=phase,  # type: ignore[arg-type]
        agent_scenario_params=params,
        scenario_params=params,
        messages=["Scenario parameters parsed; review or edit before run."],
    )


async def update_scenario_params(
    *,
    scenario_run_id: UUID,
    human_params: ScenarioParams,
    advisor_id: UUID,
) -> ScenarioUpdateParamsResponse:
    """Diff agent vs human params, bump version, status PARAMS_UPDATED."""
    row = _load_scenario_run(scenario_run_id, advisor_id=advisor_id)
    try:
        agent_params = _params_from_row(row.get("agent_scenario_params"))
    except ScenarioServiceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ScenarioServiceError(
            f"Invalid agent_scenario_params: {exc}",
            error_code=WBP_SCENARIO_PARAMS,
            http_status=400,
        ) from exc

    diffs, is_exact = diff_scenario_params(agent_params, human_params)
    version = int(row.get("scenario_params_version") or 1) + 1
    now = _now_iso()

    if diffs:
        diff_rows = [
            {
                "scenario_run_id": str(scenario_run_id),
                "advisor_id": str(advisor_id),
                "field_path": d.field_path,
                "agent_value": d.agent_value,
                "human_value": d.human_value,
                "is_exact_match": d.is_exact_match,
            }
            for d in diffs
        ]
        try:
            _client().table("scenario_param_diffs").insert(diff_rows).execute()
        except Exception as exc:  # noqa: BLE001
            _guard_db(exc)
            raise ScenarioServiceError(
                f"Failed to persist scenario_param_diffs: {exc}",
                error_code=WBP_SCENARIO_SERVER,
            ) from exc

    try:
        _client().table("scenario_runs").update(
            {
                "scenario_params": human_params.model_dump(mode="json"),
                "scenario_params_version": version,
                "status": "PARAMS_UPDATED",
                "updated_at": now,
                "error_code": None,
                "error_message": None,
            }
        ).eq("id", str(scenario_run_id)).execute()
    except Exception as exc:  # noqa: BLE001
        _guard_db(exc)
        raise ScenarioServiceError(
            f"Failed to update scenario_params: {exc}",
            error_code=WBP_SCENARIO_SERVER,
        ) from exc

    logger.info(
        "scenario_params_updated",
        scenario_run_id=str(scenario_run_id),
        version=version,
        diff_count=len(diffs),
        is_exact_match=is_exact,
    )
    write_scenario_step_audit(
        scenario_run_id=scenario_run_id,
        portfolio_id=UUID(str(row["portfolio_id"])),
        step=STEP_PARAMS_UPDATE,
        agent_or_component="human_advisor",
        advisor_id=advisor_id,
        phase=str(row.get("phase") or "phase1"),
        implementation="human",
        input_payload={
            "agent_scenario_params": agent_params.model_dump(mode="json"),
        },
        output_payload={
            "scenario_params": human_params.model_dump(mode="json"),
            "scenario_params_version": version,
            "is_exact_match": is_exact,
            "diffs": [d.model_dump(mode="json") for d in diffs],
        },
    )
    return ScenarioUpdateParamsResponse(
        scenario_run_id=scenario_run_id,
        status="PARAMS_UPDATED",
        scenario_params=human_params,
        scenario_params_version=version,
        diffs=diffs,
        is_exact_match=is_exact,
    )


def mark_run_failed(
    scenario_run_id: UUID,
    *,
    error_code: str,
    error_message: str,
    portfolio_id: UUID | None = None,
    advisor_id: UUID | None = None,
) -> None:
    try:
        _client().table("scenario_runs").update(
            {
                "status": "FAILED",
                "error_code": error_code,
                "error_message": error_message[:2000],
                "updated_at": _now_iso(),
            }
        ).eq("id", str(scenario_run_id)).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "scenario_run_fail_persist_error",
            scenario_run_id=str(scenario_run_id),
            error=str(exc),
        )
    if portfolio_id is not None:
        write_scenario_step_audit(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            step=STEP_RUN_FAILED,
            agent_or_component="scenario_orchestrator",
            advisor_id=advisor_id,
            implementation="application",
            ok=False,
            input_payload={},
            output_payload={
                "status": "FAILED",
                "error_code": error_code,
                "error_message": error_message[:2000],
            },
        )


async def execute_scenario_run(
    *,
    scenario_run_id: UUID,
    advisor_id: UUID,
    limits: TechnicalLimits | None = None,
) -> ScenarioRunResponse:
    """
    Phase-1 pipeline: history → stub strategy → risk → optimize/compliance →
    staged_trades → talking points → persist COMPLETED|FAILED.
    """
    row = _load_scenario_run(scenario_run_id, advisor_id=advisor_id)
    portfolio_id = UUID(str(row["portfolio_id"]))
    phase = str(row.get("phase") or "phase1")
    if phase not in {"phase1", "phase2"}:
        phase = "phase1"

    try:
        scenario_params = _params_from_row(row.get("scenario_params"))
    except ScenarioServiceError as exc:
        mark_run_failed(
            scenario_run_id,
            error_code=exc.error_code,
            error_message=str(exc),
            portfolio_id=portfolio_id,
            advisor_id=advisor_id,
        )
        fallback = ScenarioParams(
            user_prompt=str(row.get("user_prompt") or ""),
            scenario=ScenarioBlock(
                scenario_name="invalid",
                scenario_type="invalid",
            ),
        )
        return ScenarioRunResponse(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            status="FAILED",
            phase=phase,  # type: ignore[arg-type]
            scenario_params=fallback,
            error_code=exc.error_code,
            error_message=str(exc),
        )

    # Re-validate BASE before heavy work
    try:
        portfolio = _load_portfolio_for_scenario(portfolio_id)
    except ScenarioServiceError as exc:
        mark_run_failed(
            scenario_run_id,
            error_code=exc.error_code,
            error_message=str(exc),
            portfolio_id=portfolio_id,
            advisor_id=advisor_id,
        )
        return ScenarioRunResponse(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            status="FAILED",
            phase=phase,  # type: ignore[arg-type]
            scenario_params=scenario_params,
            error_code=exc.error_code,
            error_message=str(exc),
        )

    client_alias = _load_client_alias(
        str(portfolio["client_id"]) if portfolio.get("client_id") else None
    )

    try:
        _client().table("scenario_runs").update(
            {
                "status": "RUNNING",
                "error_code": None,
                "error_message": None,
                "updated_at": _now_iso(),
            }
        ).eq("id", str(scenario_run_id)).execute()
    except Exception as exc:  # noqa: BLE001
        _guard_db(exc)
        raise

    try:
        trading_rules = await load_active_trading_rules()
        # Prefer live configs.feature_flags so ops can flip research without re-parse.
        cfg_flags = _load_config_row("scenario_wf02").get("feature_flags") or {}
        if not isinstance(cfg_flags, dict):
            cfg_flags = {}
        run_flags = row.get("feature_flags") or {}
        if not isinstance(run_flags, dict):
            run_flags = {}
        feature_flags = {**run_flags, **cfg_flags}

        approved = approved_crypto_bases_from_rules(trading_rules)
        base_positions = _load_base_positions(portfolio_id)
        universe = build_scenario_universe(
            base_positions, approved_crypto=[str(c) for c in approved]
        )
        universe_tickers = [t for t, _ac in universe]
        params_dump = scenario_params.model_dump(mode="json")

        enable_house = bool(feature_flags.get("house_research_rag", True))
        enable_market = bool(feature_flags.get("market_research_web", True))

        if enable_house:
            house_research = await run_house_research(
                advisor_id=advisor_id,
                scenario_params=params_dump,
                universe_tickers=universe_tickers,
            )
        else:
            house_research = {
                "message_type": "in_house_research_view",
                "implementation": "disabled",
                "asset_views": [],
                "hit_count": 0,
                "messages": ["House research disabled by feature_flags."],
            }

        if enable_market:
            house_then_market = await asyncio.to_thread(
                run_market_research,
                scenario_params=params_dump,
                universe_tickers=universe_tickers,
                limits=limits,
            )
            market_research = house_then_market
        else:
            market_research = {
                "message_type": "external_research_view",
                "implementation": "disabled",
                "asset_views": [],
                "article_count": 0,
                "messages": ["Market research disabled by feature_flags."],
            }

        # Yahoo history + cvxpy are blocking — run off the event loop.
        return await asyncio.to_thread(
            _execute_phase1_sync,
            scenario_run_id=scenario_run_id,
            advisor_id=advisor_id,
            portfolio_id=portfolio_id,
            phase=phase,
            scenario_params=scenario_params,
            client_alias=client_alias,
            trading_rules=trading_rules,
            limits=limits,
            house_research=house_research,
            market_research=market_research,
            feature_flags=feature_flags,
        )

    except ScenarioServiceError as exc:
        mark_run_failed(
            scenario_run_id,
            error_code=exc.error_code,
            error_message=str(exc),
            portfolio_id=portfolio_id,
            advisor_id=advisor_id,
        )
        if exc.http_status >= 500 and exc.error_code == WBP_SCENARIO_SERVER:
            raise
        return ScenarioRunResponse(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            status="FAILED",
            phase=phase,  # type: ignore[arg-type]
            scenario_params=scenario_params,
            error_code=exc.error_code,
            error_message=str(exc),
        )
    except ComplianceUnresolvedError as exc:
        code = getattr(exc, "error_code", WBP_SCENARIO_COMPLIANCE) or WBP_SCENARIO_COMPLIANCE
        mark_run_failed(
            scenario_run_id,
            error_code=code,
            error_message=str(exc),
            portfolio_id=portfolio_id,
            advisor_id=advisor_id,
        )
        return ScenarioRunResponse(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            status="FAILED",
            phase=phase,  # type: ignore[arg-type]
            scenario_params=scenario_params,
            error_code=code,
            error_message=str(exc),
        )
    except ScenarioOptimizerError as exc:
        code = getattr(exc, "error_code", WBP_SCENARIO_RUN) or WBP_SCENARIO_RUN
        mark_run_failed(
            scenario_run_id,
            error_code=code,
            error_message=str(exc),
            portfolio_id=portfolio_id,
            advisor_id=advisor_id,
        )
        return ScenarioRunResponse(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            status="FAILED",
            phase=phase,  # type: ignore[arg-type]
            scenario_params=scenario_params,
            error_code=code,
            error_message=str(exc),
        )
    except ScenarioHistoryError as exc:
        mark_run_failed(
            scenario_run_id,
            error_code=WBP_SCENARIO_RUN,
            error_message=str(exc),
            portfolio_id=portfolio_id,
            advisor_id=advisor_id,
        )
        return ScenarioRunResponse(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            status="FAILED",
            phase=phase,  # type: ignore[arg-type]
            scenario_params=scenario_params,
            error_code=WBP_SCENARIO_RUN,
            error_message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        if _is_missing_relation(exc):
            mark_run_failed(
                scenario_run_id,
                error_code=WBP_SCENARIO_SERVER,
                error_message=_MIGRATION_HINT,
                portfolio_id=portfolio_id,
                advisor_id=advisor_id,
            )
            raise ScenarioServiceError(
                _MIGRATION_HINT,
                error_code=WBP_SCENARIO_SERVER,
                http_status=503,
            ) from exc
        logger.exception("scenario_run_unexpected", error=str(exc))
        mark_run_failed(
            scenario_run_id,
            error_code=WBP_SCENARIO_RUN,
            error_message=str(exc),
            portfolio_id=portfolio_id,
            advisor_id=advisor_id,
        )
        return ScenarioRunResponse(
            scenario_run_id=scenario_run_id,
            portfolio_id=portfolio_id,
            status="FAILED",
            phase=phase,  # type: ignore[arg-type]
            scenario_params=scenario_params,
            error_code=WBP_SCENARIO_RUN,
            error_message=str(exc),
        )


async def build_pdf_bytes(
    *,
    scenario_run_id: UUID,
    advisor_id: UUID,
) -> tuple[bytes, str, str | None]:
    """
    Render PDF from persisted run artifacts.

    Returns (pdf_bytes, filename, client_alias).
    """
    row = _load_scenario_run(scenario_run_id, advisor_id=advisor_id)
    status = str(row.get("status") or "")
    if status != "COMPLETED":
        raise ScenarioServiceError(
            f"PDF requires COMPLETED scenario_run (current status={status or 'unknown'})",
            error_code=WBP_SCENARIO_PDF,
            http_status=400,
        )

    try:
        scenario_params = _params_from_row(row.get("scenario_params"))
    except Exception as exc:  # noqa: BLE001
        raise ScenarioServiceError(
            f"Invalid scenario_params for PDF: {exc}",
            error_code=WBP_SCENARIO_PDF,
            http_status=400,
        ) from exc

    opti_raw = row.get("opti_folio") or []
    if not isinstance(opti_raw, list):
        opti_raw = []
    opti_metrics_raw = row.get("risk_metrics_opti") or {}
    base_metrics_raw = row.get("risk_metrics_base")
    talking_raw = row.get("talking_points") or []
    bullets: list[str] = []
    if isinstance(talking_raw, list):
        for item in talking_raw:
            if isinstance(item, dict) and item.get("bullet"):
                bullets.append(str(item["bullet"]))
            elif isinstance(item, str):
                bullets.append(item)

    sv_raw = row.get("strategy_view") or {}
    news_raw: list[Any] = []
    perf_raw: Any = row.get("performance_series")
    if isinstance(sv_raw, dict):
        news_raw = list(sv_raw.get("news_footnotes") or [])
        if not perf_raw:
            perf_raw = sv_raw.get("performance_series")
    footnote_lines = format_news_footnote_lines(news_raw)

    client_alias = _load_client_alias(
        str(row["client_id"]) if row.get("client_id") else None
    )
    scenario_name = scenario_params.scenario.scenario_name

    try:
        pdf = await asyncio.to_thread(
            build_scenario_pdf,
            scenario_name=scenario_name,
            opti_folio=opti_raw,
            opti_metrics=opti_metrics_raw
            if isinstance(opti_metrics_raw, dict)
            else ClientPortfolioMetrics(),
            base_metrics=base_metrics_raw if isinstance(base_metrics_raw, dict) else None,
            talking_points=bullets,
            client_alias=client_alias,
            news_footnotes=footnote_lines,
            performance_series=perf_raw if isinstance(perf_raw, dict) else None,
        )
    except Exception as exc:  # noqa: BLE001
        raise ScenarioServiceError(
            f"PDF render failed: {exc}",
            error_code=WBP_SCENARIO_PDF,
            http_status=500,
        ) from exc

    safe_name = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "_"
        for ch in scenario_name.strip()
    )[:48] or "scenario"
    filename = f"{safe_name}_{scenario_run_id.hex[:8]}.pdf"
    return pdf, filename, client_alias


__all__ = [
    "PARSE_TIMEOUT_SECONDS",
    "PARAMS_TIMEOUT_SECONDS",
    "PDF_TIMEOUT_SECONDS",
    "RUN_TIMEOUT_SECONDS",
    "ScenarioServiceError",
    "WBP_SCENARIO_TIMEOUT",
    "build_pdf_bytes",
    "execute_scenario_run",
    "mark_run_failed",
    "parse_and_create_run",
    "place_all_staged_orders",
    "update_scenario_params",
]
