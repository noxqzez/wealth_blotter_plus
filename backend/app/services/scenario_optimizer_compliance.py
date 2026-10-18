"""Trading-rules → constraints and weight compliance evaluation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Sequence

from backend.app.services.scenario_optimizer_types import (
    DEFAULT_RISK_AVERSION,
    RULE_CLIENT,
    RULE_PORTFOLIO,
    RULE_TRADE,
    TIGHTEN_CASH_STEP,
    TIGHTEN_CRYPTO_FACTOR,
    TIGHTEN_SINGLE_FACTOR,
    ComplianceViolation,
    OptimizeConstraints,
    _client_rule_applies,
    _is_approved_crypto,
    _is_cash_ticker,
    _normalize_ticker,
    normalize_rule_category,
)
from backend.app.services.trading_rules_policy import (
    approved_crypto_bases_from_rules,
    effective_max_single_weight,
    effective_min_cash_weight,
)

def constraints_from_trading_rules(
    trading_rules: Sequence[Mapping[str, Any]],
    *,
    tickers: Sequence[str],
    asset_classes: Mapping[str, str],
    client_alias: str | None = None,
    risk_aversion: float = DEFAULT_RISK_AVERSION,
    approved_crypto: Sequence[str] | None = None,
) -> OptimizeConstraints:
    """Fold active trading_rules parameters into OptimizeConstraints."""
    bases = frozenset(
        str(b).strip().upper().removesuffix("-USD")
        for b in (
            approved_crypto
            if approved_crypto is not None
            else approved_crypto_bases_from_rules(trading_rules)
        )
        if str(b).strip()
    )
    approved_tickers = frozenset(f"{b}-USD" for b in bases)

    def _approved(ticker: str) -> bool:
        t = _normalize_ticker(ticker)
        if t in approved_tickers:
            return True
        base = t[:-4] if t.endswith("-USD") else t
        return base in bases

    cash_tickers = {t for t in tickers if _is_cash_ticker(t) or asset_classes.get(t) == "CASH"}
    crypto_tickers = {
        t
        for t in tickers
        if asset_classes.get(t) == "CRYPTO" or _approved(t)
    }
    restricted: set[str] = set()
    min_cash: float | None = None
    max_single: float | None = None
    max_crypto: float | None = None
    max_crypto_single: float | None = None

    for rule in trading_rules:
        if rule.get("is_active") is False:
            continue
        category = normalize_rule_category(str(rule.get("rule_category", "")))
        params = dict(rule.get("parameters") or {})
        name = str(rule.get("rule_name", "")).strip().lower()

        if category == RULE_PORTFOLIO:
            ac = str(params.get("asset_class", "") or "").upper()
            cash_cap, _ = effective_min_cash_weight(rule)
            if cash_cap is not None:
                min_cash = cash_cap if min_cash is None else max(min_cash, cash_cap)
            single_cap, _ = effective_max_single_weight(rule)
            if single_cap is not None:
                if ac == "CRYPTO":
                    max_crypto_single = (
                        single_cap
                        if max_crypto_single is None
                        else min(max_crypto_single, single_cap)
                    )
                else:
                    max_single = (
                        single_cap
                        if max_single is None
                        else min(max_single, single_cap)
                    )
            if params.get("max_total_weight") is not None and ac == "CRYPTO":
                val = float(params["max_total_weight"])
                max_crypto = val if max_crypto is None else min(max_crypto, val)

        elif category == RULE_TRADE:
            for t in params.get("restricted_tickers") or []:
                restricted.add(_normalize_ticker(str(t)))

        elif category == RULE_CLIENT:
            # Firm-wide jurisdictions row is not a ticker restriction.
            if name == "jurisdictions":
                continue
            if not _client_rule_applies(params, client_alias):
                continue
            for t in params.get("ineligible_tickers") or []:
                restricted.add(_normalize_ticker(str(t)))

    # Never allow non-approved crypto (defense in depth).
    for t in tickers:
        if asset_classes.get(t) == "CRYPTO" and not _approved(t):
            restricted.add(t)

    return OptimizeConstraints(
        min_cash_weight=min_cash,
        max_single_weight=max_single,
        max_crypto_weight=max_crypto,
        max_crypto_single_weight=max_crypto_single,
        restricted_tickers=restricted,
        cash_tickers=cash_tickers,
        crypto_tickers=crypto_tickers,
        risk_aversion=risk_aversion,
    )


def evaluate_weight_compliance(
    weights: Mapping[str, float],
    *,
    asset_classes: Mapping[str, str],
    trading_rules: Sequence[Mapping[str, Any]],
    constraints: OptimizeConstraints,
    client_alias: str | None = None,
) -> list[ComplianceViolation]:
    """Check PORTFOLIO / TRADE / CLIENT trading_rules (legacy aliases OK)."""
    violations: list[ComplianceViolation] = []
    w = { _normalize_ticker(k): float(v) for k, v in weights.items() }

    cash_weight = sum(
        wt
        for t, wt in w.items()
        if t in constraints.cash_tickers or asset_classes.get(t) == "CASH" or _is_cash_ticker(t)
    )
    crypto_weight = sum(
        wt
        for t, wt in w.items()
        if t in constraints.crypto_tickers or asset_classes.get(t) == "CRYPTO"
    )

    for rule in trading_rules:
        if rule.get("is_active") is False:
            continue
        category = normalize_rule_category(str(rule.get("rule_category", "")))
        name = str(rule.get("rule_name", category))
        params = dict(rule.get("parameters") or {})

        if category == RULE_PORTFOLIO:
            ac = str(params.get("asset_class", "") or "").upper()
            min_cash, _ = effective_min_cash_weight(rule)
            if min_cash is None:
                min_cash = constraints.min_cash_weight
            max_single, _ = effective_max_single_weight(rule)
            if max_single is None:
                max_single = constraints.max_single_weight
            max_crypto = (
                params.get("max_total_weight")
                if ac == "CRYPTO"
                else constraints.max_crypto_weight
            )
            max_crypto_single = constraints.max_crypto_single_weight
            if ac == "CRYPTO" and max_single is not None:
                # CRYPTO asset_class single-name uses the same effective resolver
                max_crypto_single = float(max_single)
                max_single = constraints.max_single_weight
            if min_cash is not None and cash_weight + 1e-9 < float(min_cash):
                violations.append(
                    ComplianceViolation(
                        rule_category=category,
                        rule_name=name,
                        message=(
                            f"trading_rules '{name}': cash weight "
                            f"{cash_weight:.4f} below min_cash_weight "
                            f"{float(min_cash):.4f}"
                        ),
                        parameters=params,
                    )
                )
            if max_crypto is not None and crypto_weight > float(max_crypto) + 1e-9:
                violations.append(
                    ComplianceViolation(
                        rule_category=category,
                        rule_name=name,
                        message=(
                            f"trading_rules '{name}': CRYPTO sleeve "
                            f"{crypto_weight:.4f} exceeds max_total_weight "
                            f"{float(max_crypto):.4f}"
                        ),
                        parameters=params,
                    )
                )
            if max_crypto_single is not None:
                for t, wt in w.items():
                    if asset_classes.get(t) != "CRYPTO" and t not in constraints.crypto_tickers:
                        continue
                    if wt > float(max_crypto_single) + 1e-9:
                        violations.append(
                            ComplianceViolation(
                                rule_category=category,
                                rule_name=name,
                                message=(
                                    f"trading_rules '{name}': {t} weight "
                                    f"{wt:.4f} exceeds CRYPTO max_single_weight "
                                    f"{float(max_crypto_single):.4f}"
                                ),
                                parameters={**params, "ticker": t},
                            )
                        )
            if max_single is not None:
                for t, wt in w.items():
                    if _is_cash_ticker(t) or asset_classes.get(t) == "CASH":
                        continue
                    if wt > float(max_single) + 1e-9:
                        violations.append(
                            ComplianceViolation(
                                rule_category=category,
                                rule_name=name,
                                message=(
                                    f"trading_rules '{name}': {t} weight "
                                    f"{wt:.4f} exceeds max_single_weight "
                                    f"{float(max_single):.4f}"
                                ),
                                parameters={**params, "ticker": t},
                            )
                        )

        elif category == RULE_TRADE:
            for t in params.get("restricted_tickers") or []:
                nt = _normalize_ticker(str(t))
                if w.get(nt, 0.0) > 1e-9:
                    violations.append(
                        ComplianceViolation(
                            rule_category=category,
                            rule_name=name,
                            message=f"Restricted ticker {nt} has weight {w[nt]:.4f}",
                            parameters=params,
                        )
                    )

        elif category == RULE_CLIENT:
            if str(rule.get("rule_name", "")).strip().lower() == "jurisdictions":
                continue
            if not _client_rule_applies(params, client_alias):
                continue
            for t in params.get("ineligible_tickers") or []:
                nt = _normalize_ticker(str(t))
                if w.get(nt, 0.0) > 1e-9:
                    violations.append(
                        ComplianceViolation(
                            rule_category=category,
                            rule_name=name,
                            message=(
                                f"Client-ineligible ticker {nt} has weight "
                                f"{w[nt]:.4f}"
                            ),
                            parameters=params,
                        )
                    )

    # Also enforce constraint object caps when no PORTFOLIO rule rows exist.
    if not any(
        normalize_rule_category(str(r.get("rule_category", ""))) == RULE_PORTFOLIO
        for r in trading_rules
        if r.get("is_active") is not False
    ):
        if (
            constraints.min_cash_weight is not None
            and cash_weight + 1e-9 < float(constraints.min_cash_weight)
        ):
            violations.append(
                ComplianceViolation(
                    rule_category=RULE_PORTFOLIO,
                    rule_name="constraints_min_cash",
                    message=(
                        f"trading_rules: cash weight {cash_weight:.4f} below "
                        f"min_cash_weight {float(constraints.min_cash_weight):.4f}"
                    ),
                )
            )
        if (
            constraints.max_crypto_weight is not None
            and crypto_weight > float(constraints.max_crypto_weight) + 1e-9
        ):
            violations.append(
                ComplianceViolation(
                    rule_category=RULE_PORTFOLIO,
                    rule_name="constraints_max_crypto",
                    message=(
                        f"trading_rules: CRYPTO sleeve {crypto_weight:.4f} exceeds "
                        f"max_total_weight {float(constraints.max_crypto_weight):.4f}"
                    ),
                )
            )
        if constraints.max_single_weight is not None:
            for t, wt in w.items():
                if _is_cash_ticker(t) or asset_classes.get(t) == "CASH":
                    continue
                if wt > float(constraints.max_single_weight) + 1e-9:
                    violations.append(
                        ComplianceViolation(
                            rule_category=RULE_PORTFOLIO,
                            rule_name="constraints_max_single",
                            message=(
                                f"trading_rules: {t} weight {wt:.4f} exceeds "
                                f"max_single_weight "
                                f"{float(constraints.max_single_weight):.4f}"
                            ),
                            parameters={"ticker": t},
                        )
                    )
        if constraints.max_crypto_single_weight is not None:
            for t, wt in w.items():
                if asset_classes.get(t) != "CRYPTO" and t not in constraints.crypto_tickers:
                    continue
                if wt > float(constraints.max_crypto_single_weight) + 1e-9:
                    violations.append(
                        ComplianceViolation(
                            rule_category=RULE_PORTFOLIO,
                            rule_name="constraints_max_crypto_single",
                            message=(
                                f"{t} CRYPTO weight {wt:.4f} exceeds max single "
                                f"{float(constraints.max_crypto_single_weight):.4f}"
                            ),
                            parameters={"ticker": t},
                        )
                    )

    for t in constraints.restricted_tickers:
        if w.get(t, 0.0) > 1e-9:
            violations.append(
                ComplianceViolation(
                    rule_category=RULE_TRADE,
                    rule_name="constraints_restricted",
                    message=f"Restricted ticker {t} has weight {w[t]:.4f}",
                    parameters={"ticker": t},
                )
            )

    return violations


def tighten_constraints(
    constraints: OptimizeConstraints,
    violations: Sequence[ComplianceViolation],
    *,
    asset_classes: Mapping[str, str],
) -> OptimizeConstraints:
    """Tighten caps / expand restricted set based on violation categories."""
    min_cash = constraints.min_cash_weight
    max_single = constraints.max_single_weight
    max_crypto = constraints.max_crypto_weight
    max_crypto_single = constraints.max_crypto_single_weight
    restricted = set(constraints.restricted_tickers)

    for v in violations:
        if v.rule_category == RULE_PORTFOLIO:
            msg = v.message.lower()
            if "cash" in msg:
                floor = 0.0 if min_cash is None else float(min_cash)
                min_cash = min(0.95, floor + TIGHTEN_CASH_STEP)
            if "crypto weight" in msg and "max single" not in msg:
                cap = 0.10 if max_crypto is None else float(max_crypto)
                max_crypto = max(0.0, cap * TIGHTEN_CRYPTO_FACTOR)
            if "crypto weight" in msg and "max single" in msg:
                cap = (
                    0.05
                    if max_crypto_single is None
                    else float(max_crypto_single)
                )
                max_crypto_single = max(0.005, cap * TIGHTEN_SINGLE_FACTOR)
            elif "max single" in msg or "exceeds max single" in msg:
                cap = 0.25 if max_single is None else float(max_single)
                max_single = max(0.01, cap * TIGHTEN_SINGLE_FACTOR)
        elif v.rule_category in (RULE_TRADE, RULE_CLIENT):
            ticker = v.parameters.get("ticker")
            if ticker:
                restricted.add(_normalize_ticker(str(ticker)))
            for token in v.message.split():
                cand = _normalize_ticker(token)
                if cand in asset_classes or _is_approved_crypto(cand):
                    restricted.add(cand)

    return replace(
        constraints,
        min_cash_weight=min_cash,
        max_single_weight=max_single,
        max_crypto_weight=max_crypto,
        max_crypto_single_weight=max_crypto_single,
        restricted_tickers=restricted,
    )

def _coerce_constraints(
    constraints: OptimizeConstraints | Mapping[str, Any],
    tickers: Sequence[str],
) -> OptimizeConstraints:
    if isinstance(constraints, OptimizeConstraints):
        cons = constraints
    else:
        restricted = {
            _normalize_ticker(str(t))
            for t in (constraints.get("restricted_tickers") or [])
        }
        cash = {
            _normalize_ticker(str(t))
            for t in (constraints.get("cash_tickers") or [])
        }
        crypto = {
            _normalize_ticker(str(t))
            for t in (constraints.get("crypto_tickers") or [])
        }
        if not cash:
            cash = {t for t in tickers if _is_cash_ticker(t)}
        if not crypto:
            crypto = {t for t in tickers if _is_approved_crypto(t)}
        cons = OptimizeConstraints(
            min_cash_weight=(
                None
                if constraints.get("min_cash_weight") is None
                else float(constraints["min_cash_weight"])
            ),
            max_single_weight=(
                None
                if constraints.get("max_single_weight") is None
                else float(constraints["max_single_weight"])
            ),
            max_crypto_weight=(
                None
                if constraints.get("max_crypto_weight") is None
                else float(constraints["max_crypto_weight"])
            ),
            max_crypto_single_weight=(
                None
                if constraints.get("max_crypto_single_weight") is None
                else float(constraints["max_crypto_single_weight"])
            ),
            restricted_tickers=restricted,
            cash_tickers=cash,
            crypto_tickers=crypto,
            risk_aversion=float(
                constraints.get("risk_aversion", DEFAULT_RISK_AVERSION)
            ),
            fixed_weights={
                _normalize_ticker(str(k)): float(v)
                for k, v in dict(constraints.get("fixed_weights") or {}).items()
            },
        )
    # Ensure cash/crypto sets cover aliases present in tickers.
    cash = set(cons.cash_tickers) | {t for t in tickers if _is_cash_ticker(t)}
    crypto = set(cons.crypto_tickers) | {
        t for t in tickers if _is_approved_crypto(t)
    }
    return replace(cons, cash_tickers=cash, crypto_tickers=crypto)


def _constraints_to_dict(cons: OptimizeConstraints) -> dict[str, Any]:
    return {
        "min_cash_weight": cons.min_cash_weight,
        "max_single_weight": cons.max_single_weight,
        "max_crypto_weight": cons.max_crypto_weight,
        "max_crypto_single_weight": cons.max_crypto_single_weight,
        "restricted_tickers": sorted(cons.restricted_tickers),
        "cash_tickers": sorted(cons.cash_tickers),
        "crypto_tickers": sorted(cons.crypto_tickers),
        "risk_aversion": cons.risk_aversion,
        "fixed_weights": {
            k: float(v) for k, v in sorted(cons.fixed_weights.items())
        },
    }
