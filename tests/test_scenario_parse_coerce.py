"""Tests for scenario parse coerce + heuristic fallback."""

from __future__ import annotations

from backend.app.agents.scenario_crew import (
    _coerce_scenario_payload,
    heuristic_scenario_params,
)
from backend.app.schemas.scenario import ScenarioParams


def test_heuristic_brent_geopolitical() -> None:
    params = heuristic_scenario_params(
        "Geopolitical risk escalates, Brent reaches $120"
    )
    assert params.scenario.tone == "risk_off"
    assert params.scenario.severity == "high"
    assert params.detected_factors.explicit_market_levels.brent_oil_target == 120.0


def test_heuristic_nasdaq_ath_is_risk_on() -> None:
    params = heuristic_scenario_params("Nasdaq reaches all time high in November")
    assert params.scenario.tone == "risk_on"


def test_coerce_overrides_llm_tone_for_ath() -> None:
    raw = {
        "message_type": "scenario_params",
        "user_prompt": "Nasdaq reaches all time high in November",
        "scenario": {
            "scenario_name": "Nasdaq Reaches All Time High",
            "scenario_type": "equity_rally",
            "severity": "medium",
            "tone": "risk_off",
            "base_currency": "USD",
            "investment_horizon": "1M",
            "lookback_window": "3Y",
            "data_frequency": "daily",
        },
        "detected_factors": {
            "macro_factors": ["equity_rally"],
            "explicit_market_levels": {},
            "impacted_asset_classes": ["us_equities"],
        },
        "research_context_tags": ["nasdaq"],
        "downstream_instructions": {
            "for_market_data_service": "ok",
            "for_research_agents": "ok",
            "for_quant_agent": "ok",
        },
        "confidence": {
            "scenario_classification_confidence": 0.7,
            "horizon_selection_confidence": 0.7,
        },
    }
    coerced = _coerce_scenario_payload(
        raw, user_prompt="Nasdaq reaches all time high in November"
    )
    assert coerced["scenario"]["tone"] == "risk_on"


def test_coerce_placeholder_enums() -> None:
    raw = {
        "message_type": "scenario_params",
        "user_prompt": "string",
        "scenario": {
            "scenario_name": "string",
            "scenario_type": "snake_case_tag",
            "severity": "low|medium|high",
            "tone": "risk_off|risk_on|neutral",
            "base_currency": "USD",
            "investment_horizon": "1M",
            "lookback_window": "3Y",
            "data_frequency": "daily",
        },
        "detected_factors": {
            "macro_factors": ["string"],
            "explicit_market_levels": {},
            "impacted_asset_classes": ["crypto"],
        },
        "research_context_tags": ["string"],
        "downstream_instructions": {
            "for_market_data_service": "string",
            "for_research_agents": "string",
            "for_quant_agent": "string",
        },
        "confidence": {
            "scenario_classification_confidence": 0.0,
            "horizon_selection_confidence": 0.0,
        },
    }
    coerced = _coerce_scenario_payload(raw, user_prompt="Oil shock")
    params = ScenarioParams.model_validate(coerced)
    assert params.user_prompt == "Oil shock"
    assert params.scenario.severity in {"low", "medium", "high"}
    assert params.scenario.tone in {"risk_off", "risk_on", "neutral"}
