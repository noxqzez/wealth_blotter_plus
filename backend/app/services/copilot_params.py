"""Copilot scenario-parameter summary / deterministic + LLM patches."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import structlog

from backend.app.agents.llm import build_llm
from backend.app.schemas.scenario import ScenarioParams, ToneLiteral

logger = structlog.get_logger(__name__)

_BLOCKED_PARAM_KEYS = frozenset(
    {
        "coin_temperature",
        "trading_rules",
        "optimizer",
        "downstream_instructions",
        "source_agent",
        "message_type",
        "workflow_id",
        # Domain identifiers are session/server-owned — chat is read-only.
        "portfolio_id",
        "scenario_run_id",
        "scenario_id",
        "order_id",
        "position_id",
        "pos_id",
        "draft_id",
    }
)



def scenario_params_to_summary(params: ScenarioParams) -> dict[str, Any]:
    sc = params.scenario
    return {
        "scenario_name": sc.scenario_name,
        "scenario_type": sc.scenario_type,
        "severity": sc.severity,
        "tone": sc.tone,
        "investment_horizon": sc.investment_horizon,
        "lookback_window": sc.lookback_window,
        "risk_aversion": params.risk_aversion,
        "freeze_list": list(params.freeze_list),
        "weight_overrides": dict(params.weight_overrides),
        "macro_factors": list(params.detected_factors.macro_factors),
        "research_context_tags": list(params.research_context_tags),
    }


def format_scenario_params_markdown(params: ScenarioParams) -> str:
    s = scenario_params_to_summary(params)
    freeze = ", ".join(s["freeze_list"]) if s["freeze_list"] else "(none)"
    overrides = (
        ", ".join(f"{k}={v * 100:.1f}%" for k, v in s["weight_overrides"].items())
        if s["weight_overrides"]
        else "(none)"
    )
    factors = ", ".join(s["macro_factors"]) if s["macro_factors"] else "(none)"
    lines = [
        "### Scenario parameters",
        f"- **Name:** {s['scenario_name']}",
        f"- **Type:** {s['scenario_type']}",
        f"- **Tone:** {s['tone']}",
        f"- **Investment horizon:** {s['investment_horizon']}",
        f"- **Lookback window:** {s['lookback_window']}",
        f"- **Risk aversion:** {s['risk_aversion'] if s['risk_aversion'] is not None else 'default'}",
        f"- **Freeze list:** {freeze}",
        f"- **Weight overrides:** {overrides}",
        f"- **Macro factors:** {factors}",
        "",
        "Review the parameters above. You can type edits "
        "(e.g. “lookback 3Y”, “risk aversion 4”) or confirm to "
        "run optimization.",
    ]
    return "\n".join(lines)


def _apply_param_dict_patch(
    params: ScenarioParams,
    patch: dict[str, Any],
) -> ScenarioParams:
    """Apply a shallow/nested patch; reject blocked keys."""
    data = params.model_dump(mode="json")
    for key, value in patch.items():
        if key in _BLOCKED_PARAM_KEYS:
            continue
        if key == "scenario" and isinstance(value, dict):
            sc = dict(data.get("scenario") or {})
            for sk, sv in value.items():
                if sk in _BLOCKED_PARAM_KEYS:
                    continue
                sc[sk] = sv
            data["scenario"] = sc
        elif key == "detected_factors" and isinstance(value, dict):
            df = dict(data.get("detected_factors") or {})
            for fk, fv in value.items():
                if fk == "explicit_market_levels":
                    continue
                df[fk] = fv
            data["detected_factors"] = df
        elif key == "market_data_request_params" and isinstance(value, dict):
            mdr = dict(data.get("market_data_request_params") or {})
            for mk, mv in value.items():
                if mk in _BLOCKED_PARAM_KEYS:
                    continue
                mdr[mk] = mv
            data["market_data_request_params"] = mdr
        elif key in {
            "risk_aversion",
            "freeze_list",
            "weight_overrides",
            "research_context_tags",
            "user_prompt",
        }:
            data[key] = value
    # Never allow coin_temperature override via chat patch
    data["coin_temperature"] = params.coin_temperature
    return ScenarioParams.model_validate(data)


def _normalize_tone_token(raw: str) -> ToneLiteral | None:
    token = re.sub(r"[\s\-]+", "_", (raw or "").strip().lower())
    if token in {"risk_off", "riskoff", "off"}:
        return "risk_off"
    if token in {"risk_on", "riskon", "on"}:
        return "risk_on"
    if token == "neutral":
        return "neutral"
    return None


def _parse_tone_edit(lower: str) -> ToneLiteral | None:
    """Parse tone edits including 'change risk_off to risk_on' (no 'tone' required)."""
    # "change risk_off to risk_on" / "from risk off → risk on"
    m = re.search(
        r"(?:tone\s+)?(?:from\s+)?"
        r"(?:risk[_\s-]?off|risk[_\s-]?on|neutral)\s+"
        r"(?:to|->|→)\s*"
        r"(risk[_\s-]?off|risk[_\s-]?on|neutral)",
        lower,
    )
    if m:
        return _normalize_tone_token(m.group(1))
    # "tone risk_on" / "tone to risk on" / "set tone = risk_on"
    m = re.search(
        r"\btone\s*(?:to\s*|=\s*|:\s*)?(risk[_\s-]?off|risk[_\s-]?on|neutral)",
        lower,
    )
    if m:
        return _normalize_tone_token(m.group(1))
    # "make it risk_on" / "set risk on" / "use risk_on"
    m = re.search(
        r"\b(?:set|make(?:\s+it)?|use|switch(?:\s+to)?)\s+"
        r"(?:tone\s+(?:to\s+)?)?(risk[_\s-]?off|risk[_\s-]?on|neutral)\b",
        lower,
    )
    if m:
        return _normalize_tone_token(m.group(1))
    return None


def try_deterministic_param_patch(
    text: str,
    params: ScenarioParams,
) -> ScenarioParams | None:
    """Common phrase patches; return None if no match."""
    lower = text.lower().strip()
    patch: dict[str, Any] = {}
    scenario_patch: dict[str, Any] = {}

    # lookback (e.g. "lookback 3Y", "lookback window to 5y")
    lb = re.search(
        r"lookback\s*(?:window\s*)?(?:to\s*|=\s*)?(\d+)\s*([ym])",
        lower,
    )
    if lb:
        lookback = f"{lb.group(1)}{lb.group(2).upper()}"
        scenario_patch["lookback_window"] = lookback
        mdr = dict(params.market_data_request_params.model_dump())
        mdr["lookback_window"] = lookback
        patch["market_data_request_params"] = mdr

    # horizon (explicit "horizon 1Y" or temporal phrases)
    hm = re.search(
        r"(?:investment\s+)?horizon\s*(?:to\s*|=\s*)?(\d+)\s*([ymwd])",
        lower,
    )
    if hm:
        scenario_patch["investment_horizon"] = f"{hm.group(1)}{hm.group(2).upper()}"
    elif re.search(
        r"\b(?:next\s+year|over\s+the\s+next\s+year|by\s+next\s+year|"
        r"within\s+(?:a|one)\s+year|in\s+(?:a|one)\s+year)\b",
        lower,
    ):
        scenario_patch["investment_horizon"] = "1Y"
    elif (
        (m := re.search(
            r"\b(?:in|over|within|for)?\s*(?:the\s+)?next\s+(\d+)\s+months?\b",
            lower,
        ))
        and int(m.group(1)) >= 12
    ):
        scenario_patch["investment_horizon"] = "1Y"
    elif re.search(r"\b(?:by|in|through|until)\s+20\d{2}\b", lower) or re.search(
        r"\b20(2[6-9]|[3-9]\d)\b", lower
    ):
        # Future calendar year cue (2026+) → treat as ≥1Y planning horizon
        scenario_patch["investment_horizon"] = "1Y"
    elif re.search(r"\b(?:next\s+month|by\s+month[\s-]?end|this\s+month)\b", lower):
        scenario_patch["investment_horizon"] = "1M"
    elif re.search(r"\b(?:next\s+week|this\s+week)\b", lower):
        scenario_patch["investment_horizon"] = "1W"

    # severity
    for sev in ("low", "medium", "high"):
        if re.search(rf"severity\s*(?:to\s*|=\s*)?{sev}", lower) or (
            f"make it {sev}" in lower and "severity" in lower
        ):
            scenario_patch["severity"] = sev  # type: ignore[assignment]
            break
    if "severity high" in lower or "high severity" in lower:
        scenario_patch["severity"] = "high"
    if "severity low" in lower or "low severity" in lower:
        scenario_patch["severity"] = "low"
    if "severity medium" in lower or "medium severity" in lower:
        scenario_patch["severity"] = "medium"

    # tone — prefer explicit "X to Y" target so "change risk_off to risk_on" works
    parsed_tone = _parse_tone_edit(lower)
    if parsed_tone is not None:
        scenario_patch["tone"] = parsed_tone

    # risk aversion
    ram = re.search(
        r"risk[_\s-]?aversion\s*(?:to\s*|=\s*|λ\s*=\s*)?(\d+(?:\.\d+)?)",
        lower,
    )
    if ram:
        patch["risk_aversion"] = float(ram.group(1))

    # freeze list
    fm = re.search(
        r"freeze\s*(?:list\s*)?(?:to\s*|=\s*|:)?\s*([A-Za-z0-9.\-,\s]+)",
        lower,
    )
    if fm and "unfreeze" not in lower:
        raw = fm.group(1)
        tickers = [
            t.strip().upper()
            for t in re.split(r"[,;\s]+", raw)
            if t.strip() and re.fullmatch(r"[A-Z0-9.\-]{1,32}", t.strip().upper())
        ]
        # Stop at trailing words that aren't tickers
        tickers = [t for t in tickers if t not in {"TO", "AND", "THE", "LIST"}]
        if tickers:
            patch["freeze_list"] = tickers
    if re.search(r"clear\s+freeze|unfreeze\s+all|no\s+freeze", lower):
        patch["freeze_list"] = []

    if scenario_patch:
        patch["scenario"] = scenario_patch

    if not patch:
        return None
    try:
        return _apply_param_dict_patch(params, patch)
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot_param_patch_invalid", error=str(exc))
        return None


async def try_llm_param_patch(
    text: str,
    params: ScenarioParams,
) -> ScenarioParams | None:
    """Optional light LLM JSON patch; never touches blocked keys."""
    summary = scenario_params_to_summary(params)
    prompt = (
        "You patch scenario parameters for a wealth advisor Copilot.\n"
        "Return ONLY JSON with allowed fields among: "
        "scenario.scenario_name, scenario.scenario_type, scenario.severity "
        "(low|medium|high), scenario.tone (risk_off|risk_on|neutral), "
        "scenario.investment_horizon, scenario.lookback_window, "
        "risk_aversion (number), freeze_list (string[]), "
        "weight_overrides (object of ticker→fraction 0-1), "
        "research_context_tags (string[]), detected_factors.macro_factors.\n"
        "Do NOT change coin_temperature, trading rules, optimizer internals, "
        "downstream_instructions, portfolio_id, scenario_run_id, scenario_id, "
        "order_id, position_id, pos_id, draft_id, or workflow_id.\n"
        'Shape: {"patch": { ...fields... }}\n\n'
        f"Current params:\n{json.dumps(summary)}\n\n"
        f"Advisor edit request:\n{text}"
    )
    try:
        llm = build_llm(temperature=0.2)
        raw: Any = await asyncio.to_thread(llm.call, prompt)
        content = raw if isinstance(raw, str) else str(raw)
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        data = json.loads(content)
        patch = data.get("patch") if isinstance(data, dict) else None
        if not isinstance(patch, dict) or not patch:
            return None
        return _apply_param_dict_patch(params, patch)
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot_llm_param_patch_failed", error=str(exc))
        return None


# ---------------------------------------------------------------------------
