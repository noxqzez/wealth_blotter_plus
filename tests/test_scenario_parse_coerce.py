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


def test_heuristic_nasdaq_doubling_is_risk_on() -> None:
    params = heuristic_scenario_params("NASDAQ doubling over the next year")
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
    # Pipe-joined placeholder must NOT become risk_off via substring "off".
    assert params.scenario.tone == "risk_off"  # oil cue override
    coerced_neutral = _coerce_scenario_payload(
        raw, user_prompt="Mild seasonal volatility"
    )
    assert coerced_neutral["scenario"]["tone"] == "neutral"
    assert coerced_neutral["scenario"]["severity"] == "medium"


def test_mixed_prompt_tone_is_neutral() -> None:
    """Crash + rise (+ rates) must not flatten to risk_off."""
    prompt = "Solana crashes, Tencent rises and interest rates spike"
    params = heuristic_scenario_params(prompt)
    assert params.scenario.tone == "neutral"

    raw = {
        "message_type": "scenario_params",
        "user_prompt": prompt,
        "scenario": {
            "scenario_name": "Solana Crash and Tencent Rise",
            "scenario_type": "custom_scenario",
            "severity": "high",
            "tone": "risk_off",  # LLM often picks this from "crash"
            "base_currency": "USD",
            "investment_horizon": "1M",
            "lookback_window": "5Y",
            "data_frequency": "daily",
        },
        "detected_factors": {
            "macro_factors": ["crypto", "rates"],
            "explicit_market_levels": {},
            "impacted_asset_classes": ["crypto", "hk_equities"],
        },
        "research_context_tags": ["solana", "tencent"],
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
    coerced = _coerce_scenario_payload(raw, user_prompt=prompt)
    assert coerced["scenario"]["tone"] == "neutral"


def test_pure_crash_still_risk_off() -> None:
    assert heuristic_scenario_params("Solana crash").scenario.tone == "risk_off"


def test_pure_rise_is_risk_on() -> None:
    assert heuristic_scenario_params("Tencent rises sharply").scenario.tone == "risk_on"


def test_explicit_risk_on_beats_crash_token() -> None:
    assert (
        heuristic_scenario_params("risk on despite a brief crash scare").scenario.tone
        == "risk_on"
    )
