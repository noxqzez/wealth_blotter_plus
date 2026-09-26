"""Tests for WF-02 scenario prompt relevance guardrail (denylist)."""

from __future__ import annotations

import pytest

from backend.app.services.scenario_prompt_guard import (
    INVALID_PROMPT_MESSAGE,
    assert_scenario_prompt_in_scope,
    scenario_prompt_in_scope,
)


@pytest.mark.parametrize(
    "prompt",
    [
        "Nasdaq reaches all time high in November",
        "Geopolitical risk escalates, Brent reaches $120",
        "Fed rate cut and softer inflation",
        "Hurricane disrupts Gulf oil supply",
        "Election outcome and tariff policy shock",
        "BTC-USD selloff after ETF outflows",
        "0700.HK earnings miss weighs on HSI",
        "Donald trump stays president for another term in 2028",
        "Incredibly harsh winter in December with cold causing power outages in Texas",
        "Terror attack in new york sometime this year",
        "Something bad happens to markets next quarter",
        "Random macro shock with unclear details",
    ],
)
def test_in_scope_prompts(prompt: str) -> None:
    assert scenario_prompt_in_scope(prompt) is True
    assert_scenario_prompt_in_scope(prompt)  # no raise


@pytest.mark.parametrize(
    "prompt",
    [
        "I hate Mondays",
        "What should I cook for dinner?",
        "hello",
        "asdf",
        "Tell me a joke about cats",
        "lol",
    ],
)
def test_out_of_scope_prompts(prompt: str) -> None:
    assert scenario_prompt_in_scope(prompt) is False
    with pytest.raises(ValueError, match="scenario prompt"):
        assert_scenario_prompt_in_scope(prompt)
    assert "scenario prompt" in INVALID_PROMPT_MESSAGE.lower()
