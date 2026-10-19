"""WF-02 scenario optimizer: Black–Litterman + mean-variance (deterministic).

Phase-1 UAT engine — no LLM, no market-data fetch. Callers supply a returns
DataFrame and portfolio / rule context; this module returns weights and
OptiPosition-like dicts.

Assumptions (pragmatic for UAT; keep math correct within these):
- Returns columns are tickers; rows are period returns (default daily).
- Annualization uses ``TRADING_DAYS_PER_YEAR`` (252) when converting means/cov
  from period returns to annualized figures for BL / MV.
- Equilibrium / prior: historical mean of the provided returns when
  ``strategy_view`` is empty or has no usable asset views.
- Qualitative ``final_view`` strings map to fixed annualized return *tilts*
  via ``QUALITATIVE_VIEW_TILTS``; absolute BL views are prior + tilt.
- BL uses the classic absolute-view update with scalar τ and diagonal Ω
  derived from confidences (higher confidence → smaller view variance).
- Mean-variance: maximize ``μᵀw − (λ/2) wᵀΣw`` (long-only, fully invested).
- Cash proxies (``CASH``, ``CASH_USD``, and bare ``USD``) get ~0 volatility
  and ~0 expected excess return after covariance estimation.
- ``PORTFOLIO`` rules (legacy aliases: POSITION_LIMIT / CONCENTRATION):
  ``asset_class=CRYPTO`` + ``max_total_weight`` caps the CRYPTO sleeve;
  ``max_single_weight`` caps each CRYPTO name when ``asset_class=CRYPTO``,
  else global single-name. Cash is excluded from global max-single caps.
  Weight caps come only from ``trading_rules`` — never from business_guardrails.
- Investable universe = base folio tickers ∪ approved crypto
  (``BTC``/``ETH``/``DOGE``/``XRP``/``SOL``, stored as ``*-USD`` CRYPTO).
  No other coins are invented.
- Tickers without return history receive a small synthetic idiosyncratic
  series (~40% ann. vol) so LW/cvxpy stay conditioned — replace with real
  history in production runs.
- ``weight_pct`` on OptiPosition-like dicts is a **fraction** in [0, 1]
  (same convention as portfolio ``current_weight_pct`` in seeds).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from backend.app.schemas.scenario import StrategyView
from backend.app.services.scenario_optimizer_compliance import (
    _coerce_constraints,
    _constraints_to_dict,
    constraints_from_trading_rules,
    evaluate_weight_compliance,
    tighten_constraints,
)
from backend.app.services.scenario_optimizer_math import (
    black_litterman_returns,
    ledoit_wolf_cov,
    optimize_weights,
    _align_returns_to_universe,
    _as_aligned_vector,
    _ensure_symmetric_psd,
    _historical_mean_returns,
    _psd_cov,
    _solve_cvxpy,
    _validate_returns_frame,
    _zero_cash_covariance,
)
from backend.app.services.scenario_optimizer_types import (
    APPROVED_CRYPTO_BASES,
    APPROVED_CRYPTO_TICKERS,
    CASH_TICKER_ALIASES,
    DEFAULT_RISK_AVERSION,
    QUALITATIVE_VIEW_TILTS,
    ComplianceUnresolvedError,
    ComplianceViolation,
    OptimizeConstraints,
    ScenarioOptimizerError,
    _is_approved_crypto,
    _is_cash_ticker,
    _normalize_asset_class,
    _normalize_ticker,
    normalize_rule_category,
)
from backend.app.services.trading_rules_policy import (
    approved_crypto_bases_from_rules,
    trading_rules_fold_notes,
)

def propose_opti_folio(
    *,
    returns: pd.DataFrame,
    base_positions: Sequence[Mapping[str, Any]],
    strategy_view: StrategyView | Mapping[str, Any] | None = None,
    trading_rules: Sequence[Mapping[str, Any]] | None = None,
    portfolio_value: float | None = None,
    prices: Mapping[str, float] | None = None,
    ticker_currency: Mapping[str, str] | None = None,
    hkd_per_usd: float | None = None,
    client_alias: str | None = None,
    risk_aversion: float = DEFAULT_RISK_AVERSION,
    freeze_list: Sequence[str] | None = None,
    weight_overrides: Mapping[str, float] | None = None,
    max_compliance_iters: int = 5,
    max_safety_retries: int = 3,
) -> dict[str, Any]:
    """Build universe, BL posterior, optimize with compliance re-solve.

    A rule-based quant safety gate validates inputs/outputs; on failure it
    logs for tech, applies corrections (incl. strategist-view feedback), and
    retries up to ``max_safety_retries``.

    ``freeze_list`` locks tickers to their current BASE weight.
    ``weight_overrides`` sets explicit target fractions (0–1); overrides win
    over freeze when both name the same ticker.
    """
    from backend.app.services.scenario_quant_safety import (
        MAX_SAFETY_RETRIES,
        apply_input_corrections,
        apply_output_corrections,
        log_safety_failure,
        raise_safety_exhausted,
        validate_optimizer_inputs,
        validate_optimizer_outputs,
    )
    from backend.app.core.error_codes import WBP_SCENARIO_QUANT_SAFETY

    _validate_returns_frame(returns)
    messages: list[str] = []
    safety_trace: list[dict[str, Any]] = []
    retries = max(1, int(max_safety_retries or MAX_SAFETY_RETRIES))

    approved_bases = approved_crypto_bases_from_rules(trading_rules or [])
    universe = build_optimizer_universe(
        base_positions,
        returns.columns,
        approved_crypto=approved_bases,
    )
    tickers = universe["tickers"]
    asset_classes = universe["asset_classes"]
    messages.extend(universe["messages"])
    messages.append(
        f"Approved crypto universe from trading_rules: {', '.join(approved_bases)}"
    )
    messages.extend(trading_rules_fold_notes(trading_rules or []))

    aligned = _align_returns_to_universe(returns, tickers)
    cov = ledoit_wolf_cov(aligned)
    prior = _historical_mean_returns(aligned)
    messages.append("Prior expected returns = historical mean (annualized).")

    views_dict, confidences, view_notes = _views_from_strategy(
        strategy_view, prior=prior, tickers=tickers
    )
    messages.extend(view_notes)

    base_constraints = constraints_from_trading_rules(
        trading_rules or [],
        tickers=tickers,
        asset_classes=asset_classes,
        client_alias=client_alias,
        risk_aversion=risk_aversion,
        approved_crypto=approved_bases,
    )
    fixed = _resolve_fixed_weights(
        base_positions=base_positions,
        freeze_list=freeze_list or [],
        weight_overrides=weight_overrides or {},
        universe_tickers=tickers,
    )
    if fixed:
        base_constraints = replace(base_constraints, fixed_weights=fixed)
        messages.append(
            "Advisor fixed weights: "
            + ", ".join(f"{k}={v:.4f}" for k, v in sorted(fixed.items()))
        )
    if abs(float(risk_aversion) - DEFAULT_RISK_AVERSION) > 1e-12:
        messages.append(f"risk_aversion λ={float(risk_aversion):.4f} (default {DEFAULT_RISK_AVERSION}).")

    last_report = None
    weights: dict[str, float] = {}
    positions: list[dict[str, Any]] = []
    mu_series = pd.Series(prior, index=tickers)
    loop_result: dict[str, Any] = {
        "weights": {},
        "messages": [],
        "violations_resolved": [],
        "iterations": 0,
    }

    for attempt in range(1, retries + 1):
        if views_dict:
            mu = black_litterman_returns(
                prior, views_dict, confidences, cov, tickers=tickers
            )
            if attempt == 1:
                messages.append(
                    f"Black–Litterman applied with {len(views_dict)} absolute view(s)."
                )
        else:
            mu = prior.copy()
            if attempt == 1:
                messages.append(
                    "Empty strategy_view — using historical mean prior only."
                )

        mu_series = pd.Series(mu, index=tickers)
        for t in tickers:
            if _is_cash_ticker(t):
                mu_series[t] = 0.0

        in_report = validate_optimizer_inputs(
            returns=aligned,
            cov=cov,
            tickers=tickers,
            constraints=base_constraints,
            prices=prices,
            portfolio_value=portfolio_value,
            strategy_views=views_dict,
        )
        if not in_report.ok:
            log_safety_failure(in_report, stage="inputs", attempt=attempt)
            (
                aligned,
                cov,
                base_constraints,
                views_dict,
                confidences,
                corrections,
            ) = apply_input_corrections(
                returns=aligned,
                cov=cov,
                constraints=base_constraints,
                views_dict=views_dict,
                confidences=confidences,
                report=in_report,
            )
            if list(aligned.columns) != list(tickers):
                tickers = [str(c) for c in aligned.columns]
                asset_classes = {t: asset_classes.get(t, "EQUITY") for t in tickers}
                prior = _historical_mean_returns(aligned)
            safety_trace.append(
                {
                    "attempt": attempt,
                    "stage": "inputs",
                    "findings": [f.message for f in in_report.findings],
                    "feedback_for_prior": list(in_report.feedback_for_prior),
                    "corrections": corrections,
                }
            )
            messages.append(
                f"Quant safety gate (inputs, attempt {attempt}): "
                + "; ".join(in_report.feedback_for_prior[:3] or corrections[:3])
            )
            last_report = in_report
            if attempt >= retries:
                raise_safety_exhausted(in_report, attempts=attempt)
            continue

        loop_result = compliance_resolve_loop(
            expected_returns=mu_series,
            cov=cov,
            tickers=tickers,
            asset_classes=asset_classes,
            constraints=base_constraints,
            trading_rules=trading_rules or [],
            client_alias=client_alias,
            max_iters=max_compliance_iters,
        )
        weights = dict(loop_result["weights"])
        messages.extend(loop_result.get("messages", []))

        out_report = validate_optimizer_outputs(
            weights=weights, positions=[], tickers=tickers
        )
        if not out_report.ok:
            log_safety_failure(out_report, stage="outputs", attempt=attempt)
            weights, out_corr = apply_output_corrections(weights)
            safety_trace.append(
                {
                    "attempt": attempt,
                    "stage": "outputs",
                    "findings": [f.message for f in out_report.findings],
                    "feedback_for_prior": list(out_report.feedback_for_prior),
                    "corrections": out_corr,
                }
            )
            messages.append(
                f"Quant safety gate (outputs, attempt {attempt}): "
                + "; ".join(out_report.feedback_for_prior[:3] or out_corr[:3])
            )
            last_report = out_report
            # Re-check after correction
            recheck = validate_optimizer_outputs(
                weights=weights, positions=[], tickers=tickers
            )
            if not recheck.ok and attempt < retries:
                views_dict = {}
                confidences = {}
                messages.append(
                    "Safety feedback → strategist views cleared; "
                    "retry with historical prior."
                )
                continue
            if not recheck.ok and attempt >= retries:
                raise_safety_exhausted(recheck, attempts=attempt)

        positions = weights_to_opti_positions(
            weights,
            asset_classes=asset_classes,
            base_positions=base_positions,
            portfolio_value=portfolio_value,
            prices=prices,
            ticker_currency=ticker_currency,
            hkd_per_usd=hkd_per_usd,
        )
        pos_report = validate_optimizer_outputs(
            weights=weights, positions=positions, tickers=tickers
        )
        if not pos_report.ok:
            log_safety_failure(pos_report, stage="positions", attempt=attempt)
            safety_trace.append(
                {
                    "attempt": attempt,
                    "stage": "positions",
                    "findings": [f.message for f in pos_report.findings],
                    "feedback_for_prior": list(pos_report.feedback_for_prior),
                }
            )
            last_report = pos_report
            if attempt >= retries:
                raise_safety_exhausted(pos_report, attempts=attempt)
            views_dict = {}
            confidences = {}
            messages.append(
                "Safety feedback → positions invalid; retry with prior-only views."
            )
            continue

        if safety_trace:
            messages.append(f"Quant safety gate cleared after {attempt} attempt(s).")
        return {
            "positions": positions,
            "weights": weights,
            "expected_returns": {
                t: float(mu_series[t]) for t in tickers if t in mu_series.index
            },
            "messages": messages,
            "violations_resolved": loop_result.get("violations_resolved", []),
            "iterations": loop_result.get("iterations", 1),
            "safety_trace": safety_trace,
            "safety_ok": True,
        }

    if last_report is not None:
        raise_safety_exhausted(last_report, attempts=retries)
    raise ScenarioOptimizerError(
        "Quant safety gate exhausted without a feasible solution",
        error_code=WBP_SCENARIO_QUANT_SAFETY,
    )



def compliance_resolve_loop(
    *,
    expected_returns: np.ndarray | pd.Series,
    cov: np.ndarray,
    tickers: Sequence[str],
    asset_classes: Mapping[str, str],
    constraints: OptimizeConstraints | Mapping[str, Any],
    trading_rules: Sequence[Mapping[str, Any]],
    client_alias: str | None = None,
    max_iters: int = 5,
) -> dict[str, Any]:
    """Optimize; if limit/restriction rules fail, tighten and re-solve.

    Categories handled: ``PORTFOLIO``, ``TRADE``, ``CLIENT`` (legacy aliases
    accepted). After ``max_iters`` unresolved violations, raises
    ``ComplianceUnresolvedError`` (``WBP-SCENARIO-COMPLIANCE``).
    """
    if max_iters < 1:
        raise ScenarioOptimizerError(
            "max_iters must be ≥ 1",
            details={"max_iters": max_iters},
        )

    cons = _coerce_constraints(constraints, list(tickers))
    names = [str(t) for t in tickers]
    messages: list[str] = []
    resolved: list[dict[str, Any]] = []
    last_violations: list[ComplianceViolation] = []
    weights: dict[str, float] = {}

    for iteration in range(1, max_iters + 1):
        weights = optimize_weights(
            expected_returns=expected_returns,
            cov=cov,
            constraints=cons,
            tickers=names,
        )
        last_violations = evaluate_weight_compliance(
            weights,
            asset_classes=asset_classes,
            trading_rules=trading_rules,
            constraints=cons,
            client_alias=client_alias,
        )
        if not last_violations:
            messages.append(f"Compliance cleared on iteration {iteration}.")
            return {
                "weights": weights,
                "iterations": iteration,
                "messages": messages,
                "violations_resolved": resolved,
                "constraints": _constraints_to_dict(cons),
            }

        for v in last_violations:
            resolved.append(
                {
                    "iteration": iteration,
                    "rule_category": v.rule_category,
                    "rule_name": v.rule_name,
                    "message": v.message,
                }
            )
        messages.append(
            f"Iteration {iteration}: {len(last_violations)} violation(s); "
            "tightening constraints."
        )
        cons = tighten_constraints(cons, last_violations, asset_classes=asset_classes)

    raise ComplianceUnresolvedError(
        f"Compliance unresolved after {max_iters} optimize iterations",
        details={
            "max_iters": max_iters,
            "last_violations": [
                {
                    "rule_category": v.rule_category,
                    "rule_name": v.rule_name,
                    "message": v.message,
                }
                for v in last_violations
            ],
            "last_weights": weights,
            "constraints": _constraints_to_dict(cons),
        },
    )

def build_optimizer_universe(
    base_positions: Sequence[Mapping[str, Any]],
    returns_columns: Sequence[Any] | None = None,
    *,
    approved_crypto: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Universe = base folio tickers + approved crypto only."""
    messages: list[str] = []
    asset_classes: dict[str, str] = {}
    ordered: list[str] = []
    bases = frozenset(
        str(b).strip().upper().removesuffix("-USD")
        for b in (approved_crypto if approved_crypto is not None else DEFAULT_APPROVED_CRYPTO_BASES)
        if str(b).strip()
    )
    approved_tickers = frozenset(f"{b}-USD" for b in bases)

    def _approved(ticker: str) -> bool:
        t = _normalize_ticker(ticker)
        if t in approved_tickers:
            return True
        base = t[:-4] if t.endswith("-USD") else t
        return base in bases

    for pos in base_positions:
        ticker = _normalize_ticker(str(pos.get("ticker", "")))
        if not ticker:
            continue
        ac = _normalize_asset_class(str(pos.get("asset_class", "EQUITY")))
        if ac == "CRYPTO" and not _approved(ticker):
            messages.append(
                f"Dropped non-approved crypto from base folio: {ticker}"
            )
            continue
        if ticker not in asset_classes:
            ordered.append(ticker)
        asset_classes[ticker] = ac

    for crypto in sorted(approved_tickers):
        if crypto not in asset_classes:
            ordered.append(crypto)
            asset_classes[crypto] = "CRYPTO"
            messages.append(f"Added approved crypto to universe: {crypto}")

    # Optionally include return columns that are already in base/approved set.
    if returns_columns is not None:
        for col in returns_columns:
            ticker = _normalize_ticker(str(col))
            if ticker in asset_classes and ticker not in ordered:
                ordered.append(ticker)

    if not ordered:
        raise ScenarioOptimizerError(
            "optimizer universe is empty",
            details={"base_positions_count": len(base_positions)},
        )

    return {
        "tickers": ordered,
        "asset_classes": asset_classes,
        "messages": messages,
        "approved_crypto_bases": sorted(bases),
        "approved_crypto_tickers": sorted(approved_tickers),
    }

def weights_to_opti_positions(
    weights: Mapping[str, float],
    *,
    asset_classes: Mapping[str, str],
    base_positions: Sequence[Mapping[str, Any]] | None = None,
    portfolio_value: float | None = None,
    prices: Mapping[str, float] | None = None,
    ticker_currency: Mapping[str, str] | None = None,
    hkd_per_usd: float | None = None,
) -> list[dict[str, Any]]:
    """Convert weight fractions into OptiPosition-like dicts.

    ``prices`` are **local** last prices (HKD for ``*.HK``). USD notionals use
    Yahoo HKD=X; share qty = USD_notional / USD_unit_price.
    """
    from backend.app.services.market_fx import (
        currency_for_ticker,
        notional_usd,
        qty_from_usd_notional,
    )

    base_qty = {
        _normalize_ticker(str(p.get("ticker", ""))): float(p.get("quantity") or 0.0)
        for p in (base_positions or [])
        if p.get("ticker")
    }
    base_spot = {
        _normalize_ticker(str(p.get("ticker", ""))): float(p["spot_price"])
        for p in (base_positions or [])
        if p.get("ticker") and p.get("spot_price") is not None
    }
    base_ccy = {
        _normalize_ticker(str(p.get("ticker", ""))): currency_for_ticker(
            str(p.get("ticker", "")),
            str(p.get("currency") or "") or None,
        )
        for p in (base_positions or [])
        if p.get("ticker")
    }
    total_value = float(portfolio_value) if portfolio_value is not None else None
    if total_value is None and base_positions:
        notionals = [
            float(p["notional_value"])
            for p in base_positions
            if p.get("notional_value") is not None
        ]
        if notionals:
            total_value = float(sum(notionals))

    positions: list[dict[str, Any]] = []
    for ticker, weight in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0])):
        if weight <= 1e-12 and not _is_cash_ticker(ticker):
            continue
        raw_ac = asset_classes.get(ticker)
        if raw_ac is None:
            if _is_cash_ticker(ticker):
                ac = "CASH"
            elif _is_approved_crypto(ticker):
                ac = "CRYPTO"
            else:
                ac = "EQUITY"
        else:
            ac = _normalize_asset_class(str(raw_ac))

        ccy = "USD"
        if ticker_currency and ticker in ticker_currency:
            ccy = str(ticker_currency[ticker]).upper()
        elif ticker in base_ccy:
            ccy = base_ccy[ticker]
        else:
            ccy = currency_for_ticker(ticker)

        local_px: float | None = None
        if prices is not None and ticker in prices and float(prices[ticker]) > 0:
            local_px = float(prices[ticker])
        elif ticker in base_spot and base_spot[ticker] > 0:
            local_px = base_spot[ticker]

        notional: float | None = None
        quantity = 0.0
        if total_value is not None:
            notional = float(total_value) * float(weight)
            if local_px is not None and float(local_px) > 0:
                quantity = qty_from_usd_notional(
                    notional,
                    local_px,
                    ccy,
                    hkd_per_usd=hkd_per_usd,
                )
            elif ticker in base_qty and weight > 0:
                quantity = base_qty[ticker]
            elif ac == "CASH":
                quantity = notional
        elif ticker in base_qty:
            quantity = base_qty[ticker]

        # Equity: whole shares (#,###). Crypto: 2 dp (#,###.##). Cash: 2 dp face.
        if ac == "CASH":
            qty_out = round(float(quantity), 2)
        elif ac == "CRYPTO":
            qty_out = round(float(quantity), 2)
        else:
            qty_out = float(int(quantity))  # truncate toward 0

        # Keep notional consistent with displayed qty (deterministic, no LLM).
        if local_px is not None and float(local_px) > 0 and qty_out > 0:
            notional = notional_usd(
                qty_out, local_px, ccy, hkd_per_usd=hkd_per_usd
            )
        elif qty_out <= 0 and ac != "CASH":
            # Dust after rounding — omit from opti_folio (avoids qty=0 + positive $).
            continue

        positions.append(
            {
                "ticker": ticker,
                "asset_class": ac,
                "quantity": qty_out,
                "weight_pct": round(float(weight), 6),
                "notional_value": (
                    round(float(notional), 2) if notional is not None else None
                ),
                "spot_price": round(float(local_px), 6) if local_px is not None else None,
                "currency": "HKD" if ccy == "HKD" else "USD",
            }
        )
    return positions


def qualitative_view_to_tilt(view_text: str) -> float:
    """Map qualitative strategist text to a fixed annualized return tilt."""
    text = (view_text or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return 0.0
    # Exact / contained key match; prefer longer keys first.
    for key in sorted(QUALITATIVE_VIEW_TILTS.keys(), key=len, reverse=True):
        if key in text or text == key:
            return QUALITATIVE_VIEW_TILTS[key]
    # Loose word forms
    if "bull" in text:
        return QUALITATIVE_VIEW_TILTS["bullish"]
    if "bear" in text:
        return QUALITATIVE_VIEW_TILTS["bearish"]
    return 0.0

def _resolve_fixed_weights(
    *,
    base_positions: Sequence[Mapping[str, Any]],
    freeze_list: Sequence[str],
    weight_overrides: Mapping[str, float],
    universe_tickers: Sequence[str],
) -> dict[str, float]:
    """Merge freeze (BASE weight) + overrides (explicit fraction). Overrides win."""
    universe = {_normalize_ticker(str(t)) for t in universe_tickers}
    base_w: dict[str, float] = {}
    for pos in base_positions:
        t = _normalize_ticker(str(pos.get("ticker", "")))
        if not t:
            continue
        raw = pos.get("current_weight_pct")
        if raw is None:
            continue
        try:
            base_w[t] = float(raw)
        except (TypeError, ValueError):
            continue

    fixed: dict[str, float] = {}
    for raw in freeze_list:
        t = _normalize_ticker(str(raw))
        if not t or t not in universe:
            continue
        if t in base_w:
            fixed[t] = float(max(0.0, min(1.0, base_w[t])))
        else:
            fixed[t] = 0.0

    for raw_t, raw_w in weight_overrides.items():
        t = _normalize_ticker(str(raw_t))
        if not t or t not in universe:
            continue
        try:
            wt = float(raw_w)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(wt):
            continue
        # Accept percent form (>1 and ≤100) from callers that forgot to scale.
        if wt > 1.0 and wt <= 100.0:
            wt = wt / 100.0
        fixed[t] = float(max(0.0, min(1.0, wt)))

    return fixed

def _views_from_strategy(
    strategy_view: StrategyView | Mapping[str, Any] | None,
    *,
    prior: np.ndarray,
    tickers: Sequence[str],
) -> tuple[dict[str, float], dict[str, float], list[str]]:
    notes: list[str] = []
    if strategy_view is None:
        return {}, {}, notes

    if isinstance(strategy_view, StrategyView):
        asset_views = strategy_view.asset_views
        coin_temp = strategy_view.coin_temperature
    else:
        raw_views = strategy_view.get("asset_views") or []
        asset_views = list(raw_views)
        coin_temp = strategy_view.get("coin_temperature")

    if not asset_views:
        return {}, {}, notes

    prior_map = {t: float(prior[i]) for i, t in enumerate(tickers)}
    views: dict[str, float] = {}
    confidences: dict[str, float] = {}

    from backend.app.services.coin_temperature import (
        coin_temperature_scale,
        normalize_coin_temperature,
    )

    dial = normalize_coin_temperature(coin_temp)
    temp_scale = coin_temperature_scale(dial)
    notes.append(
        f"coin_temperature dial={dial}/5 → CRYPTO tilt scale={temp_scale:.3f} (3=neutral)."
    )

    for av in asset_views:
        if isinstance(av, Mapping):
            asset = str(av.get("asset") or "")
            view_text = str(av.get("final_view") or av.get("view") or "")
            conviction = float(av.get("final_conviction", av.get("conviction", 0.5)))
        else:
            asset = str(getattr(av, "asset", ""))
            view_text = str(getattr(av, "final_view", "") or "")
            conviction = float(getattr(av, "final_conviction", 0.5))

        ticker = _normalize_ticker(asset)
        if ticker not in prior_map:
            # Allow bare BTC etc. already normalized; skip unknown names.
            notes.append(f"Skipping strategy view for out-of-universe asset: {asset}")
            continue

        tilt = qualitative_view_to_tilt(view_text)
        if _is_approved_crypto(ticker):
            tilt *= temp_scale
        views[ticker] = prior_map[ticker] + tilt
        confidences[ticker] = min(1.0, max(0.0, conviction))
        notes.append(
            f"View {ticker}: tilt={tilt:.4f} (text={view_text!r}, "
            f"conviction={conviction:.2f})"
        )

    return views, confidences, notes


__all__ = [
    "APPROVED_CRYPTO_BASES",
    "APPROVED_CRYPTO_TICKERS",
    "CASH_TICKER_ALIASES",
    "DEFAULT_RISK_AVERSION",
    "QUALITATIVE_VIEW_TILTS",
    "normalize_rule_category",
    "ComplianceUnresolvedError",
    "ComplianceViolation",
    "OptimizeConstraints",
    "ScenarioOptimizerError",
    "black_litterman_returns",
    "build_optimizer_universe",
    "compliance_resolve_loop",
    "constraints_from_trading_rules",
    "evaluate_weight_compliance",
    "ledoit_wolf_cov",
    "optimize_weights",
    "propose_opti_folio",
    "qualitative_view_to_tilt",
    "tighten_constraints",
    "weights_to_opti_positions",
]
