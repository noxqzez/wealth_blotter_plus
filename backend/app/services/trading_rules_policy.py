"""Canonical business policy extracted from ``trading_rules`` (not configs).

Position / universe / jurisdiction rules live in ``trading_rules``.
``business_guardrails`` keeps only soft product knobs (coin_temperature,
confidence_threshold).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from backend.app.schemas.rules import JurisdictionRules

# Fallback when the PORTFOLIO rule row is missing (tests / cold start only).
DEFAULT_APPROVED_CRYPTO_BASES: tuple[str, ...] = (
    "BTC",
    "ETH",
    "DOGE",
    "XRP",
    "SOL",
)

RULE_MAX_SINGLE_NAME = "max_single_name_weight"
RULE_MIN_CASH_BUFFER = "min_cash_buffer"
RULE_APPROVED_CRYPTO = "approved_crypto_universe"
RULE_JURISDICTIONS = "jurisdictions"

_PORTFOLIO_ALIASES = frozenset(
    {"PORTFOLIO", "POSITION_LIMIT", "CONCENTRATION", "WEIGHT"}
)


def _normalize_category(raw: str) -> str:
    key = str(raw or "").strip().upper().replace(" ", "_")
    if key in _PORTFOLIO_ALIASES:
        return "PORTFOLIO"
    if key in {"CLIENT", "CLIENT_RESTRICTION"}:
        return "CLIENT"
    if key in {"TRADE", "TRADING_RESTRICTION"}:
        return "TRADE"
    if key in {"TECHNICAL"}:
        return "TECHNICAL"
    return key


def _active_rules(
    trading_rules: Sequence[Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for rule in trading_rules or []:
        if rule.get("is_active") is False:
            continue
        out.append(rule)
    return out


def _rule_by_name(
    trading_rules: Sequence[Mapping[str, Any]] | None,
    name: str,
) -> Mapping[str, Any] | None:
    key = name.strip().lower()
    for rule in _active_rules(trading_rules):
        if str(rule.get("rule_name", "")).strip().lower() == key:
            return rule
    return None


def approved_crypto_bases_from_rules(
    trading_rules: Sequence[Mapping[str, Any]] | None,
    *,
    fallback: bool = True,
) -> list[str]:
    """PORTFOLIO ``approved_crypto_universe`` → base symbols (BTC, ETH, …)."""
    rule = _rule_by_name(trading_rules, RULE_APPROVED_CRYPTO)
    if rule is not None:
        cat = _normalize_category(str(rule.get("rule_category", "")))
        if cat in {"PORTFOLIO", "TRADE", ""}:
            params = dict(rule.get("parameters") or {})
            raw = (
                params.get("approved_tickers")
                or params.get("universe")
                or params.get("bases")
            )
            if isinstance(raw, list) and raw:
                bases: list[str] = []
                seen: set[str] = set()
                for item in raw:
                    base = str(item).strip().upper()
                    if base.endswith("-USD"):
                        base = base[:-4]
                    if not base or base in seen:
                        continue
                    seen.add(base)
                    bases.append(base)
                if bases:
                    return bases
    if fallback:
        return list(DEFAULT_APPROVED_CRYPTO_BASES)
    return []


def approved_crypto_tickers_from_rules(
    trading_rules: Sequence[Mapping[str, Any]] | None,
    *,
    fallback: bool = True,
) -> frozenset[str]:
    bases = approved_crypto_bases_from_rules(trading_rules, fallback=fallback)
    return frozenset(f"{b}-USD" for b in bases)


def jurisdictions_from_rules(
    trading_rules: Sequence[Mapping[str, Any]] | None,
) -> JurisdictionRules:
    """CLIENT ``jurisdictions`` → allowed markets / regulator flags."""
    rule = _rule_by_name(trading_rules, RULE_JURISDICTIONS)
    if rule is None:
        return JurisdictionRules()
    params = dict(rule.get("parameters") or {})
    nested = params.get("jurisdictions")
    payload = nested if isinstance(nested, dict) else params
    try:
        return JurisdictionRules.model_validate(
            {
                "sfc_enabled": payload.get("sfc_enabled", True),
                "sec_enabled": payload.get("sec_enabled", True),
                "allowed_markets": payload.get(
                    "allowed_markets", ["us", "hk", "crypto"]
                ),
            }
        )
    except Exception:
        return JurisdictionRules()


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def effective_max_single_weight(
    rule: Mapping[str, Any],
) -> tuple[float | None, list[str]]:
    """Resolve single-name cap from one trading_rules row.

    Both ``max_value`` and ``parameters.max_single_weight`` are trading_rules
    fields. When both are set they must agree; if they diverge, apply the
    **stricter** (smaller) cap and emit a conflict note.
    """
    notes: list[str] = []
    params = dict(rule.get("parameters") or {})
    name = str(rule.get("rule_name", "portfolio_rule"))
    candidates: list[tuple[str, float]] = []
    mv = _as_float(rule.get("max_value"))
    if mv is not None:
        candidates.append(("max_value", mv))
    pw = _as_float(params.get("max_single_weight"))
    if pw is not None:
        candidates.append(("parameters.max_single_weight", pw))
    if not candidates:
        return None, notes
    values = [v for _, v in candidates]
    effective = min(values)
    if len({round(v, 10) for v in values}) > 1:
        detail = ", ".join(f"{src}={val:.4f}" for src, val in candidates)
        notes.append(
            f"trading_rules '{name}' conflict ({detail}); "
            f"applying stricter max_single_weight={effective:.4f}"
        )
    return effective, notes


def effective_min_cash_weight(
    rule: Mapping[str, Any],
) -> tuple[float | None, list[str]]:
    """Resolve min cash from ``min_value`` and/or ``parameters.min_cash_weight``.

    When both are set and differ, apply the **stricter** (larger) floor.
    """
    notes: list[str] = []
    params = dict(rule.get("parameters") or {})
    name = str(rule.get("rule_name", "portfolio_rule"))
    candidates: list[tuple[str, float]] = []
    mv = _as_float(rule.get("min_value"))
    if mv is not None:
        candidates.append(("min_value", mv))
    pw = _as_float(params.get("min_cash_weight"))
    if pw is not None:
        candidates.append(("parameters.min_cash_weight", pw))
    if not candidates:
        return None, notes
    values = [v for _, v in candidates]
    effective = max(values)
    if len({round(v, 10) for v in values}) > 1:
        detail = ", ".join(f"{src}={val:.4f}" for src, val in candidates)
        notes.append(
            f"trading_rules '{name}' conflict ({detail}); "
            f"applying stricter min_cash_weight={effective:.4f}"
        )
    return effective, notes


def max_single_name_weight_from_rules(
    trading_rules: Sequence[Mapping[str, Any]] | None,
) -> float | None:
    """Strictest max single-name weight across active PORTFOLIO rules."""
    effective: float | None = None
    for rule in _active_rules(trading_rules):
        if _normalize_category(str(rule.get("rule_category", ""))) != "PORTFOLIO":
            continue
        params = dict(rule.get("parameters") or {})
        ac = str(params.get("asset_class", "") or "").upper()
        if ac == "CRYPTO":
            continue
        val, _ = effective_max_single_weight(rule)
        if val is None:
            continue
        effective = val if effective is None else min(effective, val)
    return effective


def min_cash_buffer_from_rules(
    trading_rules: Sequence[Mapping[str, Any]] | None,
) -> float | None:
    """Strictest min cash floor across active PORTFOLIO rules."""
    effective: float | None = None
    for rule in _active_rules(trading_rules):
        if _normalize_category(str(rule.get("rule_category", ""))) != "PORTFOLIO":
            continue
        val, _ = effective_min_cash_weight(rule)
        if val is None:
            continue
        effective = val if effective is None else max(effective, val)
    return effective


def trading_rules_fold_notes(
    trading_rules: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    """Human-readable notes when folding trading_rules into optimizer caps."""
    notes: list[str] = []
    max_single = max_single_name_weight_from_rules(trading_rules)
    min_cash = min_cash_buffer_from_rules(trading_rules)
    for rule in _active_rules(trading_rules):
        if _normalize_category(str(rule.get("rule_category", ""))) != "PORTFOLIO":
            continue
        _, conflict = effective_max_single_weight(rule)
        notes.extend(conflict)
        _, cash_conflict = effective_min_cash_weight(rule)
        notes.extend(cash_conflict)
    if max_single is not None:
        notes.append(
            f"trading_rules → optimizer max_single_weight={max_single:.4f}"
        )
    if min_cash is not None:
        notes.append(
            f"trading_rules → optimizer min_cash_weight={min_cash:.4f}"
        )
    return notes
