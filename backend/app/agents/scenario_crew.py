"""WF-02 scenario agents: parse NL → ScenarioParams; Phase-1 stub strategy_view."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import structlog
import yaml
from crewai import Agent, Crew, Process, Task

from backend.app.agents.llm import build_llm
from backend.app.agents.self_healing import (
    SelfHealExhaustedError,
    _extract_json,
    run_with_self_healing,
)
from backend.app.schemas.scenario import (
    CoinTemperatureLiteral,
    DetectedFactors,
    DownstreamInstructions,
    ExplicitMarketLevels,
    MarketDataRequestParams,
    ScenarioBlock,
    ScenarioConfidence,
    ScenarioParams,
    StrategyAssetView,
    StrategyView,
)
from backend.app.services.coin_temperature import (
    coin_temperature_from_config,
    coin_temperature_scale,
    normalize_coin_temperature,
)

logger = structlog.get_logger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent / "config"

# Concrete example — do NOT use "string" / "low|medium|high" placeholders (Ollama echoes them).
_EXAMPLE_JSON = {
    "message_type": "scenario_params",
    "user_prompt": "Geopolitical risk escalates, Brent reaches $120",
    "scenario": {
        "scenario_name": "Geopolitical Escalation with Oil Price Shock",
        "scenario_type": "geopolitical_oil_shock",
        "severity": "high",
        "tone": "risk_off",
        "base_currency": "USD",
        "investment_horizon": "1M",
        "lookback_window": "5Y",
        "data_frequency": "daily",
    },
    "detected_factors": {
        "macro_factors": ["geopolitical_risk", "oil_price_shock"],
        "explicit_market_levels": {"brent_oil_target": 120},
        "impacted_asset_classes": ["us_equities", "hk_equities", "crypto", "cash"],
    },
    "market_data_request_params": {
        "lookback_window": "5Y",
        "frequency": "daily",
        "required_fields": ["adjusted_close", "dividend_yield", "volume"],
        "include_cash_proxy": True,
    },
    "research_context_tags": [
        "geopolitical_risk",
        "oil_shock",
        "brent_120",
        "risk_off",
        "crypto",
    ],
    "downstream_instructions": {
        "for_market_data_service": (
            "Retrieve adjusted close for portfolio tickers plus approved crypto universe."
        ),
        "for_research_agents": (
            "Retrieve views on geopolitical risk, oil shock, equities, and crypto."
        ),
        "for_quant_agent": (
            "Derive numeric shocks separately; do not invent portfolio weights here."
        ),
    },
    "confidence": {
        "scenario_classification_confidence": 0.85,
        "horizon_selection_confidence": 0.7,
    },
}

_EXAMPLE_JSON_RISK_ON = {
    "message_type": "scenario_params",
    "user_prompt": "NASDAQ doubles over the next year on AI boom",
    "scenario": {
        "scenario_name": "Nasdaq Doubling on AI Boom",
        "scenario_type": "equity_rally",
        "severity": "high",
        "tone": "risk_on",
        "base_currency": "USD",
        "investment_horizon": "1Y",
        "lookback_window": "5Y",
        "data_frequency": "daily",
    },
    "detected_factors": {
        "macro_factors": ["equity_rally", "ai_boom"],
        "explicit_market_levels": {},
        "impacted_asset_classes": ["us_equities", "crypto"],
    },
    "market_data_request_params": {
        "lookback_window": "5Y",
        "frequency": "daily",
        "required_fields": ["adjusted_close", "dividend_yield", "volume"],
        "include_cash_proxy": True,
    },
    "research_context_tags": ["nasdaq", "ai_boom", "risk_on", "equity_rally"],
    "downstream_instructions": {
        "for_market_data_service": (
            "Retrieve adjusted close for portfolio tickers plus approved crypto universe."
        ),
        "for_research_agents": (
            "Retrieve views on equity rally, AI boom, and risk-on positioning."
        ),
        "for_quant_agent": (
            "Derive numeric shocks separately; do not invent portfolio weights here."
        ),
    },
    "confidence": {
        "scenario_classification_confidence": 0.8,
        "horizon_selection_confidence": 0.7,
    },
}


def _load_yaml(name: str) -> dict[str, Any]:
    path = _CONFIG_DIR / name
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {name}")
    return data


def _build_agent(key: str) -> Agent:
    agents_cfg = _load_yaml("scenario_agents.yaml")
    raw = agents_cfg.get(key)
    if not isinstance(raw, dict):
        raise KeyError(f"{key} missing from scenario_agents.yaml")
    temp = raw.get("temperature")
    return Agent(
        role=str(raw.get("role", key)),
        goal=str(raw.get("goal", "")),
        backstory=str(raw.get("backstory", "")),
        llm=build_llm(temperature=float(temp) if temp is not None else None),
        allow_delegation=False,
        verbose=True,
    )


def _prompt_tone_override(user_prompt: str) -> str | None:
    """Strong lexical cues beat LLM tone mistakes for Phase-1 UAT.

    Conflicting upside + downside cues (e.g. one name crashes while another
    rises, or risk-off shocks mixed with rallies) resolve to ``neutral`` so
    mixed client scenarios are not flattened to risk_off by a single token.
    Explicit ``risk on`` / ``risk off`` phrases still win first.
    """
    lower = (user_prompt or "").lower()
    # Explicit advisor phrases win first.
    if re.search(r"\brisk[\s_-]*on\b", lower):
        return "risk_on"
    if re.search(r"\brisk[\s_-]*off\b", lower):
        return "risk_off"
    if re.search(r"\b(mixed|balanced)\s+tone\b", lower) or re.search(
        r"\btone\s*[:=]?\s*mixed\b", lower
    ):
        return "neutral"

    off_hit = _prompt_has_risk_off_cues(lower)
    on_hit = _prompt_has_risk_on_cues(lower)
    if off_hit and on_hit:
        return "neutral"
    if off_hit:
        return "risk_off"
    if on_hit:
        return "risk_on"
    return None


_RISK_OFF_KEYWORDS: tuple[str, ...] = (
    "selloff",
    "sell-off",
    "crash",
    "crashes",
    "crashed",
    "plung",  # plunge / plunges / plunged
    "collapse",
    "recession",
    "bear market",
    "drawdown",
    "geopolit",
    "sanction",
    "oil shock",
    "oil-price",
    "brent",
    "wti",
    "rate hike",
    "rates hike",
    "rate spike",
    "rates spike",
    "interest rate spike",
    "interest rates spike",
    "hawkish",
)


_RISK_ON_KEYWORDS: tuple[str, ...] = (
    "all time high",
    "all-time high",
    "record high",
    "rally",
    "surge",
    "soar",
    "melt-up",
    "melt up",
    "bull market",
    "bullish",
    "euphoria",
    "breakout",
    "meltup",
)


def _prompt_has_risk_off_cues(lower: str) -> bool:
    if any(k in lower for k in _RISK_OFF_KEYWORDS):
        return True
    if re.search(r"\b(crash|plunge|collapse|recession)(es|ed|ing)?\b", lower):
        return True
    return False


def _prompt_has_risk_on_cues(lower: str) -> bool:
    if any(k in lower for k in _RISK_ON_KEYWORDS):
        return True
    if re.search(r"\b(ath)\b", lower):
        return True
    if re.search(r"(reach|hit|hits|hitting|at).{0,24}\bhigh\b", lower):
        return True
    # Upside price moves: doubling / 2x / triples / moonshots / rises.
    if re.search(
        r"\b(doubl\w*|tripl\w*|2x|3x|ten[- ]bagger|moon(?:shot)?)\b",
        lower,
    ):
        return True
    if re.search(
        r"\b(rise|rises|rising|rose|gain|gains|gaining|upside|outperform\w*)\b",
        lower,
    ):
        return True
    if re.search(
        r"\b(nasdaq|s&p|s&p500|spx|equit(?:y|ies)|tech)\b.{0,40}\b"
        r"(up|rise|rising|gains?|gain|boom|expand\w*)\b",
        lower,
    ):
        return True
    return False


def _coerce_scenario_payload(data: object, *, user_prompt: str) -> dict[str, Any]:
    """Normalize common Ollama mistakes before Pydantic validate."""
    if not isinstance(data, dict):
        raise ValueError("scenario payload must be a JSON object")
    out = dict(data)
    out["message_type"] = "scenario_params"
    out["user_prompt"] = user_prompt

    scenario = out.get("scenario")
    if not isinstance(scenario, dict):
        scenario = {}
    scenario = dict(scenario)

    severity = str(scenario.get("severity", "medium")).strip().lower()
    # Placeholder enums like "low|medium|high" must not pick a substring match.
    if "|" in severity or severity in {"string", "snake_case_tag", ""}:
        severity = "medium"
    elif severity not in {"low", "medium", "high"}:
        if severity.startswith("high") or severity.endswith("high"):
            severity = "high"
        elif severity.startswith("low") or severity.endswith("low"):
            severity = "low"
        else:
            severity = "medium"
    scenario["severity"] = severity

    tone = str(scenario.get("tone", "neutral")).strip().lower().replace(" ", "_")
    # Placeholder "risk_off|risk_on|neutral" contains "off" — never treat as risk_off.
    if "|" in tone or tone in {"string", "snake_case_tag", ""}:
        tone = "neutral"
    elif tone not in {"risk_off", "risk_on", "neutral"}:
        if tone in {"riskoff", "off"} or tone.endswith("_off"):
            tone = "risk_off"
        elif tone in {"riskon", "on"} or tone.endswith("_on"):
            tone = "risk_on"
        else:
            tone = "neutral"
    # Deterministic prompt cues override LLM mislabels (e.g. ATH → risk_on).
    prompt_tone = _prompt_tone_override(user_prompt)
    if prompt_tone is not None:
        tone = prompt_tone
    scenario["tone"] = tone
    scenario.setdefault("base_currency", "USD")
    scenario.setdefault("investment_horizon", "1M")
    scenario.setdefault("lookback_window", "5Y")
    scenario.setdefault("data_frequency", "daily")
    if not str(scenario.get("scenario_name") or "").strip() or scenario.get(
        "scenario_name"
    ) in {"string", "snake_case_tag"}:
        scenario["scenario_name"] = "Market scenario"
    stype = str(scenario.get("scenario_type") or "custom_scenario").strip().lower()
    if stype in {"string", "snake_case_tag", ""}:
        stype = "custom_scenario"
    scenario["scenario_type"] = re.sub(r"[^a-z0-9_]+", "_", stype).strip("_") or "custom_scenario"
    out["scenario"] = scenario

    factors = out.get("detected_factors")
    if not isinstance(factors, dict):
        factors = {}
    factors = dict(factors)
    for key in ("macro_factors", "impacted_asset_classes"):
        val = factors.get(key)
        if isinstance(val, str):
            factors[key] = [val] if val and val != "string" else []
        elif not isinstance(val, list):
            factors[key] = []
        else:
            factors[key] = [str(x) for x in val if str(x) != "string"]
    levels = factors.get("explicit_market_levels")
    if not isinstance(levels, dict):
        factors["explicit_market_levels"] = {}
    out["detected_factors"] = factors

    tags = out.get("research_context_tags")
    if isinstance(tags, str):
        out["research_context_tags"] = [tags] if tags != "string" else []
    elif isinstance(tags, list):
        out["research_context_tags"] = [str(t) for t in tags if str(t) != "string"]
    else:
        out["research_context_tags"] = []

    mdr = out.get("market_data_request_params")
    if not isinstance(mdr, dict):
        mdr = {}
    mdr = dict(mdr)
    mdr.setdefault("lookback_window", scenario.get("lookback_window", "5Y"))
    mdr.setdefault("frequency", "daily")
    mdr.setdefault(
        "required_fields", ["adjusted_close", "dividend_yield", "volume"]
    )
    mdr.setdefault("include_cash_proxy", True)
    out["market_data_request_params"] = mdr

    di = out.get("downstream_instructions")
    if not isinstance(di, dict):
        di = {}
    di = dict(di)
    for key in (
        "for_market_data_service",
        "for_research_agents",
        "for_quant_agent",
    ):
        val = di.get(key)
        if not isinstance(val, str) or val.strip() in {"", "string"}:
            di[key] = "Use scenario context; derive numerics in deterministic services."
    out["downstream_instructions"] = di

    conf = out.get("confidence")
    if not isinstance(conf, dict):
        conf = {}
    conf = dict(conf)
    for key in (
        "scenario_classification_confidence",
        "horizon_selection_confidence",
    ):
        try:
            conf[key] = float(conf.get(key, 0.5))
        except (TypeError, ValueError):
            conf[key] = 0.5
        conf[key] = max(0.0, min(1.0, float(conf[key])))
    out["confidence"] = conf
    out.setdefault("source_agent", "scenario_agent")
    return out


def heuristic_scenario_params(user_prompt: str) -> ScenarioParams:
    """Deterministic Phase-1 fallback when Ollama JSON is invalid / too slow."""
    text = user_prompt.strip()
    lower = text.lower()
    tone: str = "neutral"
    severity: str = "medium"
    factors: list[str] = []
    tags: list[str] = []
    levels: dict[str, float] = {}

    if any(k in lower for k in ("geopolit", "war", "sanction", "conflict")):
        factors.append("geopolitical_risk")
        tags.append("geopolitical_risk")
        tone = "risk_off"
        severity = "high"
    if any(k in lower for k in ("oil", "brent", "wti", "energy")):
        factors.append("oil_price_shock")
        tags.extend(["oil_shock", "energy"])
        tone = "risk_off"
        severity = "high"
    if any(k in lower for k in ("rate", "fed", "inflation", "cpi")):
        factors.append("rates_inflation")
        tags.append("rates")
    if any(k in lower for k in ("crypto", "bitcoin", "btc", "eth")):
        tags.append("crypto")
    if "risk on" in lower or "risk-on" in lower:
        tone = "risk_on"
    if "risk off" in lower or "risk-off" in lower:
        tone = "risk_off"
    prompt_tone = _prompt_tone_override(text)
    if prompt_tone is not None:
        tone = prompt_tone

    brent = re.search(r"brent[^\d]{0,20}(\d{2,3})", lower)
    if brent:
        levels["brent_oil_target"] = float(brent.group(1))
        tags.append(f"brent_{brent.group(1)}")

    if not factors:
        factors = ["macro_uncertainty"]
    if not tags:
        tags = ["custom_scenario"]

    name = text[:80].strip() or "Market scenario"
    stype = "custom_scenario"
    if "oil" in lower or "brent" in lower:
        stype = "geopolitical_oil_shock" if "geopolit" in lower else "oil_price_shock"
    elif "geopolit" in lower:
        stype = "geopolitical_risk"

    return ScenarioParams(
        user_prompt=text,
        source_agent="scenario_agent_heuristic",
        scenario=ScenarioBlock(
            scenario_name=name if len(name) > 8 else "Market scenario",
            scenario_type=stype,
            severity=severity,  # type: ignore[arg-type]
            tone=tone,  # type: ignore[arg-type]
        ),
        detected_factors=DetectedFactors(
            macro_factors=factors,
            explicit_market_levels=ExplicitMarketLevels.model_validate(levels),
            impacted_asset_classes=["us_equities", "hk_equities", "crypto", "cash"],
        ),
        market_data_request_params=MarketDataRequestParams(),
        research_context_tags=tags,
        downstream_instructions=DownstreamInstructions(
            for_market_data_service=(
                "Use portfolio tickers plus approved crypto; 5Y daily adjusted close."
            ),
            for_research_agents="Retrieve views relevant to the scenario tags.",
            for_quant_agent="Derive numeric tilts in deterministic Python services.",
        ),
        confidence=ScenarioConfidence(
            scenario_classification_confidence=0.55,
            horizon_selection_confidence=0.6,
        ),
    )


async def parse_scenario_prompt(
    user_prompt: str,
    *,
    workflow_id: str | None = None,
) -> ScenarioParams:
    """
    NL scenario → structured ScenarioParams via scenario_agent + self-heal.

    Falls back to deterministic heuristic params if the LLM JSON is invalid
    (common with small local Ollama models) so Excel UAT can proceed.
    """
    prompt_text = user_prompt.strip()
    if not prompt_text:
        raise ValueError("user_prompt is empty")

    agent = _build_agent("scenario_agent")
    tasks_cfg = _load_yaml("scenario_tasks.yaml")
    task_raw = tasks_cfg.get("parse_scenario") or {}
    description = str(task_raw.get("description", "")).strip()
    expected = str(task_raw.get("expected_output", "ScenarioParams"))
    description = (
        f"{description}\n\n"
        f"Advisor scenario prompt:\n{prompt_text}\n\n"
        "Rules:\n"
        "- Fill real values from the prompt (never the words 'string' or 'low|medium|high').\n"
        "- severity must be exactly one of: low, medium, high "
        "(never write 'low|medium|high').\n"
        "- tone must be exactly one of: risk_off, risk_on, neutral "
        "(never write 'risk_off|risk_on|neutral').\n"
        "- Prefer risk_on for upside/rally/ATH/doubling scenarios; "
        "risk_off for crash/selloff/recession/oil-war shocks; "
        "neutral when the prompt mixes upside and downside "
        "(e.g. one asset crashes while another rises, or rates spike "
        "alongside a rally); else neutral.\n"
        "- Copy user_prompt from the advisor prompt verbatim.\n"
        "- Return ONLY a JSON object (no markdown fences, no explanation).\n\n"
        "Example A (risk_off oil shock):\n"
        f"{json.dumps(_EXAMPLE_JSON)}\n\n"
        "Example B (risk_on equity upside):\n"
        f"{json.dumps(_EXAMPLE_JSON_RISK_ON)}\n\n"
        f"Return ONLY valid JSON matching `{expected}`."
    )

    task = Task(
        name="parse_scenario",
        description=description,
        expected_output="A single JSON object for ScenarioParams — no prose",
        agent=agent,
    )
    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=True,
    )

    async def _caller(ctx: str) -> str:
        # Mirror doc_classify: first call runs the crew; repairs use a focused agent.
        if ctx != "__initial__":
            heal_llm = build_llm()
            heal_agent = Agent(
                role="Schema Repair Agent",
                goal="Emit valid ScenarioParams JSON only.",
                backstory="Corrects malformed scenario_params JSON for institutional workflows.",
                llm=heal_llm,
                allow_delegation=False,
                verbose=False,
            )
            heal_task = Task(
                name="scenario_schema_repair",
                description=(
                    f"{ctx}\n\n"
                    "Fix the JSON. severity must be exactly low, medium, or high; "
                    "tone must be exactly risk_off, risk_on, or neutral. "
                    "Never emit pipe-joined enums like risk_off|risk_on|neutral. "
                    "No placeholder strings. JSON only.\n"
                    f"Example risk_off:\n{json.dumps(_EXAMPLE_JSON)}\n"
                    f"Example risk_on:\n{json.dumps(_EXAMPLE_JSON_RISK_ON)}"
                ),
                expected_output="Valid ScenarioParams JSON only",
                agent=heal_agent,
            )
            heal_crew = Crew(
                agents=[heal_agent],
                tasks=[heal_task],
                process=Process.sequential,
                verbose=False,
            )
            result = await heal_crew.kickoff_async()
            raw_text = str(result)
        else:
            result = await crew.kickoff_async()
            raw_text = str(result)

        # Coerce common local-LLM mistakes so Pydantic can accept the payload.
        try:
            coerced = _coerce_scenario_payload(
                _extract_json(raw_text), user_prompt=prompt_text
            )
            return json.dumps(coerced)
        except Exception:  # noqa: BLE001 — let self-heal see the raw text
            return raw_text

    try:
        raw_params = await run_with_self_healing(
            schema=ScenarioParams,
            prompt="__initial__",
            caller=_caller,
            max_retries=1,
        )
        # Re-validate through coerce path is unnecessary if already valid;
        # still overwrite authoritative fields.
        params = raw_params
        # Soft re-parse via coerce if enums somehow slipped through as weird strings
        try:
            coerced = _coerce_scenario_payload(
                params.model_dump(mode="json"), user_prompt=prompt_text
            )
            params = ScenarioParams.model_validate(coerced)
        except Exception:  # noqa: BLE001 — keep validated raw_params
            params = raw_params.model_copy(
                update={"user_prompt": prompt_text, "source_agent": "scenario_agent"}
            )
        source = "scenario_agent"
    except (SelfHealExhaustedError, Exception) as exc:  # noqa: BLE001
        logger.warning(
            "scenario_parse_fallback_heuristic",
            error=str(exc)[:500],
        )
        params = heuristic_scenario_params(prompt_text)
        source = "scenario_agent_heuristic"

    updates: dict[str, Any] = {
        "user_prompt": prompt_text,
        "source_agent": source,
    }
    if workflow_id:
        updates["workflow_id"] = workflow_id
    params = params.model_copy(update=updates)
    logger.info(
        "scenario_params_parsed",
        scenario_type=params.scenario.scenario_type,
        severity=params.scenario.severity,
        lookback=params.scenario.lookback_window,
        source=source,
    )
    return params


def build_stub_strategy_view(
    *,
    universe_tickers: Sequence[str],
    coin_temperature: CoinTemperatureLiteral | int | str = 2,
    scenario_tone: str = "neutral",
) -> StrategyView:
    """
    Phase-1 stub strategy_view (no LLM). Neutral/cautious tilts by tone.

    Extensible: replace with house RAG + market research consolidation when
    ``feature_flags.strategist_stub_ok`` is false / Phase 2.
    """
    tone = scenario_tone.strip().lower()
    if tone == "risk_off":
        equity_view, crypto_view, conv = "cautious", "neutral_to_cautious", 0.55
        summary = "Risk-off stub: prefer quality equities and capped crypto sleeve."
    elif tone == "risk_on":
        equity_view, crypto_view, conv = (
            "neutral_to_moderately_positive",
            "moderately_positive",
            0.55,
        )
        summary = (
            "Risk-on stub: modest risk-asset tilt; crypto still within approved universe."
        )
    else:
        equity_view, crypto_view, conv = "neutral", "neutral", 0.5
        summary = "Neutral stub strategy_view for Phase-1 UAT."

    dial = normalize_coin_temperature(coin_temperature)
    temp_scale = coin_temperature_scale(dial)
    crypto_conv = min(0.85, conv * temp_scale)

    views: list[StrategyAssetView] = []
    for raw in universe_tickers:
        ticker = str(raw).strip().upper()
        if not ticker or ticker in {"CASH", "CASH_USD", "USD", "HKD"}:
            continue
        is_crypto = ticker.endswith("-USD") or ticker in {
            "BTC",
            "ETH",
            "DOGE",
            "XRP",
            "SOL",
        }
        views.append(
            StrategyAssetView(
                asset=ticker,
                final_view=crypto_view if is_crypto else equity_view,
                final_conviction=float(crypto_conv if is_crypto else conv),
                internal_view=crypto_view if is_crypto else equity_view,
                external_view=None,
                conflict_resolution="Phase-1 stub (no external research).",
                rationale=summary,
            )
        )

    return StrategyView(
        source="stub",
        asset_views=views,
        summary=summary,
        coin_temperature=dial,  # type: ignore[arg-type]
        notes=[
            f"coin_temperature dial={dial}/5 (scale={temp_scale:.3f}; 3=neutral).",
            "Phase-1 stub strategy_view — replace with strategist_agent when Phase 2 enabled.",
        ],
    )


# Re-export for callers that imported from scenario_crew historically.
__all__ = [
    "build_stub_strategy_view",
    "coin_temperature_from_config",
    "parse_scenario_prompt",
]
