"""Blocking Phase-1 scenario execute core (history → optimize → persist)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

import structlog

from backend.app.agents.scenario_crew import (
    build_stub_strategy_view,
    coin_temperature_from_config,
)
from backend.app.agents.sector_tilts import apply_sector_tilt_overrides
from backend.app.core.error_codes import WBP_SCENARIO_SERVER
from backend.app.schemas.rules import TechnicalLimits
from backend.app.schemas.scenario import (
    OptiPosition,
    PastAnalogueArtifact,
    ScenarioParams,
    ScenarioRunResponse,
)
from backend.app.services.market_fx import (
    currency_for_ticker,
    fetch_hkd_per_usd,
)
from backend.app.services.market_history import snapshot_scenario_market_prices
from backend.app.services.past_analogue import (
    past_analogue_apply_enabled,
    past_analogue_enabled,
    past_analogue_knobs,
    run_past_analogue_pipeline,
)
from backend.app.services.risk_engine import (
    compute_client_metrics_from_weights,
    metrics_delta,
)
from backend.app.services.scenario_audit import (
    STEP_COMPLIANCE,
    STEP_CONTENT,
    STEP_HOUSE_RESEARCH,
    STEP_MARKET_HISTORY,
    STEP_MARKET_RESEARCH,
    STEP_PAST_ANALOGUE,
    STEP_QUANT,
    STEP_QUANT_SAFETY,
    STEP_RISK_BASE,
    STEP_RISK_OPTI,
    STEP_RUN_COMPLETE,
    STEP_STRATEGIST,
    write_scenario_step_audit,
)
from backend.app.services.scenario_db import (
    ScenarioServiceError,
    _MIGRATION_HINT,
    _client,
    _guard_db,
    _is_missing_relation,
    _load_base_positions,
    _load_config_row,
    _now_iso,
)
from backend.app.services.scenario_history import (
    build_scenario_universe,
    fetch_scenario_history,
    parse_lookback_days,
)
from backend.app.services.scenario_optimizer import (
    DEFAULT_RISK_AVERSION,
    propose_opti_folio,
)
from backend.app.services.scenario_performance import (
    DEFAULT_CHART_LOOKBACK_DAYS,
    DEFAULT_CHART_LOOKBACK_WINDOW,
    PerformanceBuildError,
    build_base_vs_opti_performance,
)
from backend.app.services.scenario_place import _persist_staged_trades
from backend.app.services.scenario_research import consolidate_strategy_view
from backend.app.services.scenario_talking_points import (
    build_talking_points,
    extract_news_footnotes,
)
from backend.app.services.scenario_trades import (
    _build_staged_trades,
    _latest_prices_from_bars,
    _position_maps,
)
from backend.app.services.ticker_normalize import normalize_ticker as _normalize_ticker
from backend.app.services.trading_rules_policy import approved_crypto_bases_from_rules

logger = structlog.get_logger(__name__)


def _execute_phase1_sync(
    *,
    scenario_run_id: UUID,
    advisor_id: UUID,
    portfolio_id: UUID,
    phase: str,
    scenario_params: ScenarioParams,
    client_alias: str | None,
    trading_rules: Sequence[Mapping[str, Any]],
    limits: TechnicalLimits | None,
    house_research: Mapping[str, Any],
    market_research: Mapping[str, Any],
    feature_flags: Mapping[str, Any],
) -> ScenarioRunResponse:
    """Blocking Phase-1 core (Yahoo + risk + optimize). Called via to_thread."""
    messages: list[str] = []
    audit_kw: dict[str, Any] = {
        "scenario_run_id": scenario_run_id,
        "portfolio_id": portfolio_id,
        "advisor_id": advisor_id,
        "client_alias": client_alias,
        "phase": phase,
    }
    base_positions = _load_base_positions(portfolio_id)
    base_weights, base_qty, base_ac, portfolio_value = _position_maps(base_positions)

    approved = approved_crypto_bases_from_rules(trading_rules)
    from backend.app.services.coin_temperature import normalize_coin_temperature

    # Management crypto dial: configs.business_guardrails (Streamlit later).
    # Optional per-run override only when ScenarioParams.coin_temperature is set.
    raw_coin = getattr(scenario_params, "coin_temperature", None)
    if raw_coin is not None:
        coin_temp = normalize_coin_temperature(raw_coin)
    else:
        coin_temp = coin_temperature_from_config(
            _load_config_row("business_guardrails")
        )

    universe = build_scenario_universe(
        base_positions, approved_crypto=[str(c) for c in approved]
    )
    lookback = parse_lookback_days(
        scenario_params.scenario.lookback_window
        or scenario_params.market_data_request_params.lookback_window
        or DEFAULT_CHART_LOOKBACK_WINDOW
    )
    # Always fetch at least the PDF chart window (5Y) so performance series
    # stays full-length even if the scenario/optimizer window is shorter.
    lookback = max(int(lookback), int(DEFAULT_CHART_LOOKBACK_DAYS))
    scenario_cfg = _load_config_row("scenario_wf02")
    # Past Analogue (flagged off by default): extend panel to archive length.
    if past_analogue_enabled(feature_flags, scenario_cfg):
        knobs_pre = past_analogue_knobs(scenario_cfg)
        lookback = max(int(lookback), int(knobs_pre.get("archive_days") or 2920))
    history = fetch_scenario_history(
        universe, lookback_days=lookback, limits=limits
    )
    # Keep cash-proxy skips in audit only — not advisor-facing warnings.
    messages.extend(
        m
        for m in history.messages
        if "Skipped cash proxy" not in m
    )

    write_scenario_step_audit(
        **audit_kw,
        step=STEP_MARKET_HISTORY,
        agent_or_component="market_history_service",
        implementation="yahoo_eod",
        input_payload={
            "universe": [{"ticker": t, "asset_class": ac} for t, ac in universe],
            "lookback_days": lookback,
            "scenario_params": scenario_params.model_dump(mode="json"),
        },
        output_payload={
            "n_price_rows": int(len(history.prices)),
            "n_return_rows": int(len(history.returns)),
            "tickers": list(history.prices.columns.astype(str)),
            "provider_symbols": history.provider_symbols,
            "bar_count": len(history.bars),
        },
        messages=list(history.messages),
    )

    try:
        snapshot_scenario_market_prices(
            scenario_run_id=scenario_run_id,
            bars=history.bars,
            advisor_id=advisor_id,
            also_upsert_canonical=True,
        )
    except Exception as snap_exc:  # noqa: BLE001
        if _is_missing_relation(snap_exc):
            raise ScenarioServiceError(
                _MIGRATION_HINT,
                error_code=WBP_SCENARIO_SERVER,
                http_status=503,
            ) from snap_exc
        messages.append(f"Market price snapshot skipped: {snap_exc}")
        logger.warning(
            "scenario_price_snapshot_failed",
            scenario_run_id=str(scenario_run_id),
            error=str(snap_exc),
        )

    # House / market research payloads produced before this sync block.
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_HOUSE_RESEARCH,
        agent_or_component="house_research_agent",
        implementation=str(house_research.get("implementation") or "stub"),
        input_payload={
            "scenario_params": scenario_params.model_dump(mode="json"),
            "universe_tickers": [t for t, _ in universe],
        },
        output_payload=dict(house_research),
        messages=list(house_research.get("messages") or []),
    )
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_MARKET_RESEARCH,
        agent_or_component="market_research_agent",
        implementation=str(market_research.get("implementation") or "stub"),
        input_payload={
            "scenario_params": scenario_params.model_dump(mode="json"),
            "universe_tickers": [t for t, _ in universe],
        },
        output_payload=dict(market_research),
        messages=list(market_research.get("messages") or []),
    )

    use_stub_strategist = bool(feature_flags.get("strategist_stub_ok", False))
    if use_stub_strategist:
        strategy_view = build_stub_strategy_view(
            universe_tickers=[t for t, _ac in universe],
            coin_temperature=coin_temp,
            scenario_tone=scenario_params.scenario.tone,
            user_prompt=scenario_params.user_prompt or "",
        )
        strategist_impl = "stub"
    else:
        strategy_view = consolidate_strategy_view(
            house_research=house_research,
            market_research=market_research,
            universe_tickers=[t for t, _ac in universe],
            coin_temperature=coin_temp,
            scenario_tone=scenario_params.scenario.tone,
        )
        strategist_impl = "in_house_priority_merge"
    # Lexical sector themes (China/HK, tech, energy, …) overlay both stub and
    # full strategist so empty RAG/news still honors explicit region cues.
    strategy_view = apply_sector_tilt_overrides(
        strategy_view, scenario_params.user_prompt or ""
    )
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_STRATEGIST,
        agent_or_component="strategist_agent",
        implementation=strategist_impl,
        input_payload={
            "house_research": {
                "implementation": house_research.get("implementation"),
                "hit_count": house_research.get("hit_count"),
                "asset_views": house_research.get("asset_views"),
            },
            "market_research": {
                "implementation": market_research.get("implementation"),
                "article_count": market_research.get("article_count"),
                "asset_views": market_research.get("asset_views"),
            },
            "coin_temperature": coin_temp,
            "scenario_tone": scenario_params.scenario.tone,
        },
        output_payload={"strategy_view": strategy_view.model_dump(mode="json")},
        messages=list(strategy_view.notes),
    )

    base_metrics = compute_client_metrics_from_weights(history.returns, base_weights)
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_RISK_BASE,
        agent_or_component="risk_engine",
        implementation="deterministic",
        input_payload={"weights": base_weights, "n_return_rows": int(len(history.returns))},
        output_payload={"base_client_metrics": base_metrics.model_dump(mode="json")},
    )

    prices = _latest_prices_from_bars(history.bars)
    # Keep local last prices (HKD for *.HK). Prefer BASE blotter spots when present.
    ticker_ccy: dict[str, str] = {}
    for pos in base_positions:
        t = _normalize_ticker(str(pos.get("ticker", "")))
        if not t:
            continue
        ticker_ccy[t] = currency_for_ticker(
            t, str(pos.get("currency") or "") or None
        )
        spot = pos.get("spot_price")
        if spot is not None and float(spot) > 0:
            prices[t] = float(spot)

    hkd_per_usd = fetch_hkd_per_usd(limits=limits)
    messages.append(f"FX HKD=X={hkd_per_usd:.6f} (USD notionals only; last price local)")

    quant_input = {
        "strategy_view": strategy_view.model_dump(mode="json"),
        "base_positions": [
            {
                "ticker": p.get("ticker"),
                "asset_class": p.get("asset_class"),
                "quantity": p.get("quantity"),
                "current_weight_pct": p.get("current_weight_pct"),
                "spot_price": p.get("spot_price"),
                "currency": p.get("currency"),
                "notional_value": p.get("notional_value"),
            }
            for p in base_positions
        ],
        "trading_rules": [
            {
                "rule_name": r.get("rule_name"),
                "rule_category": r.get("rule_category"),
                "parameters": r.get("parameters"),
                "is_active": r.get("is_active"),
            }
            for r in trading_rules
        ],
        "portfolio_value": portfolio_value,
        "price_tickers": sorted(prices.keys()),
        "hkd_per_usd": hkd_per_usd,
        "n_return_rows": int(len(history.returns)),
    }

    opti = propose_opti_folio(
        returns=history.returns,
        base_positions=base_positions,
        strategy_view=strategy_view,
        trading_rules=list(trading_rules),
        portfolio_value=portfolio_value or None,
        prices=prices,
        ticker_currency=ticker_ccy,
        hkd_per_usd=hkd_per_usd,
        client_alias=client_alias,
        risk_aversion=float(
            scenario_params.risk_aversion
            if scenario_params.risk_aversion is not None
            else DEFAULT_RISK_AVERSION
        ),
        freeze_list=list(scenario_params.freeze_list or []),
        weight_overrides=dict(scenario_params.weight_overrides or {}),
    )
    messages.extend(list(opti.get("messages") or []))

    opti_weights: dict[str, float] = {
        _normalize_ticker(str(k)): float(v)
        for k, v in (opti.get("weights") or {}).items()
    }
    safety_trace = list(opti.get("safety_trace") or [])
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_QUANT_SAFETY,
        agent_or_component="quant_safety_gate",
        implementation="rule_based",
        input_payload={
            "max_safety_retries": 3,
            "feedback_loops": len(safety_trace),
        },
        output_payload={
            "safety_ok": bool(opti.get("safety_ok", True)),
            "safety_trace": safety_trace,
        },
        messages=[
            m
            for m in (opti.get("messages") or [])
            if "safety" in str(m).lower()
        ]
        or ["Quant safety gate passed."],
    )
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_QUANT,
        agent_or_component="quant_agent_optimizer",
        implementation="black_litterman_cvxpy",
        input_payload=quant_input,
        output_payload={
            "weights": opti_weights,
            "positions": list(opti.get("positions") or []),
            "expected_returns": opti.get("expected_returns"),
            "iterations": opti.get("iterations"),
            "messages": list(opti.get("messages") or []),
            "safety_ok": bool(opti.get("safety_ok", True)),
        },
        messages=list(opti.get("messages") or []),
    )
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_COMPLIANCE,
        agent_or_component="compliance_resolve_loop",
        implementation="deterministic",
        input_payload={
            "trading_rules": quant_input["trading_rules"],
            "iterations": opti.get("iterations"),
        },
        output_payload={
            "violations_resolved": opti.get("violations_resolved"),
            "weights": opti_weights,
            "messages": list(opti.get("messages") or []),
        },
        messages=[
            m
            for m in (opti.get("messages") or [])
            if "compliance" in str(m).lower() or "constraint" in str(m).lower()
        ]
        or ["Compliance loop finished."],
    )

    opti_metrics = compute_client_metrics_from_weights(history.returns, opti_weights)
    delta = metrics_delta(base_metrics, opti_metrics)
    write_scenario_step_audit(
        **audit_kw,
        step=STEP_RISK_OPTI,
        agent_or_component="risk_engine",
        implementation="deterministic",
        input_payload={"weights": opti_weights},
        output_payload={
            "opti_client_metrics": opti_metrics.model_dump(mode="json"),
            "metrics_delta": delta,
        },
    )

    # Past Analogue — opt-in, fail-open (easy rollback: flag off → no Tavily / no tilts).
    past_analogue_payload: dict[str, Any] | None = None
    pa_positions_override: list[dict[str, Any]] | None = None
    if past_analogue_enabled(feature_flags, scenario_cfg):
        try:
            knobs = past_analogue_knobs(scenario_cfg)
            apply_pa = past_analogue_apply_enabled(feature_flags, scenario_cfg)
            pa_artifact = run_past_analogue_pipeline(
                params=scenario_params,
                returns=history.returns,
                base_weights=base_weights,
                applied_weights=opti_weights,
                strategy_view=strategy_view,
                base_positions=base_positions,
                trading_rules=list(trading_rules),
                portfolio_value=portfolio_value or None,
                prices=prices,
                ticker_currency=ticker_ccy,
                hkd_per_usd=hkd_per_usd,
                client_alias=client_alias,
                risk_aversion=float(
                    scenario_params.risk_aversion
                    if scenario_params.risk_aversion is not None
                    else DEFAULT_RISK_AVERSION
                ),
                freeze_list=list(scenario_params.freeze_list or []),
                weight_overrides=dict(scenario_params.weight_overrides or {}),
                knobs=knobs,
                apply=apply_pa,
            )
            past_analogue_payload = pa_artifact.model_dump(mode="json")
            messages.extend(list(pa_artifact.messages or []))
            # Applied path: weights already ran propose_opti_folio with trading_rules.
            if apply_pa and pa_artifact.applied and pa_artifact.weights_shadow:
                opti_weights = {
                    _normalize_ticker(str(k)): float(v)
                    for k, v in pa_artifact.weights_shadow.items()
                }
                if pa_artifact.positions_shadow:
                    pa_positions_override = list(pa_artifact.positions_shadow)
                opti_metrics = compute_client_metrics_from_weights(
                    history.returns, opti_weights
                )
                delta = metrics_delta(base_metrics, opti_metrics)
                messages.append(
                    "past_analogue: applied blended strategist+analogue weights to "
                    "live opti (trading/compliance enforced in both opti legs)"
                )
            write_scenario_step_audit(
                **audit_kw,
                step=STEP_PAST_ANALOGUE,
                agent_or_component="past_analogue_agent",
                implementation="web_search_llm_extract_apply",
                input_payload={
                    "knobs": knobs,
                    "apply_requested": apply_pa,
                    "user_prompt": (scenario_params.user_prompt or "")[:500],
                },
                output_payload=past_analogue_payload,
                messages=list(pa_artifact.messages or []),
            )
        except Exception as pa_exc:  # noqa: BLE001
            logger.warning(
                "past_analogue_hook_failed",
                scenario_run_id=str(scenario_run_id),
                error=str(pa_exc),
            )
            messages.append(f"past_analogue fail-open: {pa_exc}")
            past_analogue_payload = {
                "enabled": True,
                "applied": False,
                "shadow": True,
                "messages": [f"past_analogue fail-open: {pa_exc}"],
                "chat_summary": "weight input from historical events = 0.000",
            }
            write_scenario_step_audit(
                **audit_kw,
                step=STEP_PAST_ANALOGUE,
                agent_or_component="past_analogue_agent",
                implementation="fail_open",
                input_payload={},
                output_payload=past_analogue_payload,
                messages=[str(pa_exc)],
                ok=False,
            )

    opti_position_dicts: list[dict[str, Any]] = (
        list(pa_positions_override)
        if pa_positions_override is not None
        else list(opti.get("positions") or [])
    )
    opti_positions = [OptiPosition.model_validate(p) for p in opti_position_dicts]

    staged = _build_staged_trades(
        scenario_run_id=scenario_run_id,
        base_qty=base_qty,
        base_weights=base_weights,
        opti_positions=opti_position_dicts,
        asset_classes=base_ac,
        client_alias=client_alias,
    )
    _persist_staged_trades(
        scenario_run_id=scenario_run_id,
        advisor_id=advisor_id,
        portfolio_id=portfolio_id,
        trades=staged,
        client_alias=client_alias,
    )

    talking = build_talking_points(
        metrics_delta=delta,
        base_metrics=base_metrics,
        staged_trades=staged,
        strategy_view=strategy_view,
        opti_folio=opti_positions,
        max_market_bullets=3,
    )
    news_footnotes = extract_news_footnotes(
        list(market_research.get("articles") or []),
        limit=5,
    )

    performance_payload: dict[str, Any] | None = None
    try:
        price_panel = history.prices_panel
        if price_panel is None or price_panel.empty:
            price_panel = history.prices
        perf = build_base_vs_opti_performance(
            price_panel,
            base_weights=base_weights,
            opti_weights=opti_weights,
            lookback_window=DEFAULT_CHART_LOOKBACK_WINDOW,
            target_lookback_days=DEFAULT_CHART_LOOKBACK_DAYS,
        )
        performance_payload = perf.model_dump(mode="json")
        if not perf.validation_ok:
            messages.append(
                "Performance series validation warnings: "
                + "; ".join(perf.validation_messages)
            )
        else:
            messages.append(
                f"Performance series OK ({perf.point_count_full} points, "
                f"lookback={perf.lookback_window or 'n/a'})."
            )
    except PerformanceBuildError as exc:
        messages.append(f"Performance series unavailable: {exc}")
        logger.warning(
            "scenario_performance_build_failed",
            scenario_run_id=str(scenario_run_id),
            error=str(exc),
        )

    write_scenario_step_audit(
        **audit_kw,
        step=STEP_CONTENT,
        agent_or_component="content_agent",
        implementation="deterministic_perf_and_trade_gated_color",
        input_payload={
            "metrics_delta": delta,
            "staged_trade_tickers": [t.ticker for t in staged],
            "strategy_view_source": strategy_view.source,
            "news_footnote_count": len(news_footnotes),
        },
        output_payload={
            "talking_points": [t.model_dump(mode="json") for t in talking],
            "news_footnotes": news_footnotes,
            "performance_series": performance_payload,
        },
    )

    strategy_view_json = strategy_view.model_dump(mode="json")
    strategy_view_json["notes"] = list(strategy_view.notes) + [
        f"metrics_delta={json.dumps(delta)}"
    ]
    # Persist for PDF footnotes (jsonb; ignored by StrategyView validation).
    strategy_view_json["news_footnotes"] = news_footnotes
    if performance_payload is not None:
        # Backup if performance_series column not yet migrated.
        strategy_view_json["performance_series"] = performance_payload
    # Nested jsonb — no new DB column required (easy rollback).
    if past_analogue_payload is not None:
        strategy_view_json["past_analogue"] = past_analogue_payload

    artifact: dict[str, Any] = {
        "status": "COMPLETED",
        "strategy_view": strategy_view_json,
        "risk_metrics_base": base_metrics.model_dump(mode="json"),
        "risk_metrics_opti": opti_metrics.model_dump(mode="json"),
        "opti_folio": [p.model_dump(mode="json") for p in opti_positions],
        "talking_points": [t.model_dump(mode="json") for t in talking],
        "performance_series": performance_payload,
        "error_code": None,
        "error_message": None,
        "updated_at": _now_iso(),
    }

    try:
        _client().table("scenario_runs").update(artifact).eq(
            "id", str(scenario_run_id)
        ).execute()
    except Exception as exc:  # noqa: BLE001
        # Soft fallback if performance_series column not migrated yet.
        err = str(exc).lower()
        if "performance_series" in err and "performance_series" in artifact:
            artifact.pop("performance_series", None)
            try:
                _client().table("scenario_runs").update(artifact).eq(
                    "id", str(scenario_run_id)
                ).execute()
                messages.append(
                    "performance_series column missing — apply "
                    "migration_performance_series.sql; chart data kept in-run only."
                )
            except Exception as exc2:  # noqa: BLE001
                _guard_db(exc2)
                raise
        else:
            _guard_db(exc)
            raise

    write_scenario_step_audit(
        **audit_kw,
        step=STEP_RUN_COMPLETE,
        agent_or_component="scenario_orchestrator",
        implementation="application",
        input_payload={},
        output_payload={
            "status": "COMPLETED",
            "opti_folio": [p.model_dump(mode="json") for p in opti_positions],
            "staged_trades": [t.model_dump(mode="json") for t in staged],
            "metrics_delta": delta,
            "performance_series": performance_payload,
            "past_analogue": past_analogue_payload,
        },
        messages=messages,
    )

    logger.info(
        "scenario_run_completed",
        scenario_run_id=str(scenario_run_id),
        staged_trades=len(staged),
        opti_positions=len(opti_positions),
    )
    past_analogue_model = None
    if past_analogue_payload is not None:
        try:
            past_analogue_model = PastAnalogueArtifact.model_validate(
                past_analogue_payload
            )
        except Exception:  # noqa: BLE001
            past_analogue_model = None
    return ScenarioRunResponse(
        scenario_run_id=scenario_run_id,
        portfolio_id=portfolio_id,
        status="COMPLETED",
        phase=phase,  # type: ignore[arg-type]
        scenario_params=scenario_params,
        strategy_view=strategy_view,
        base_client_metrics=base_metrics,
        opti_folio=opti_positions,
        opti_client_metrics=opti_metrics,
        metrics_delta=delta,
        staged_trades=staged,
        talking_points=talking,
        performance_series=performance_payload,
        past_analogue=past_analogue_model,
        messages=messages,
    )

