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

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

import cvxpy as cp
import numpy as np
import pandas as pd
from sklearn.covariance import LedoitWolf

from backend.app.core.error_codes import WBP_SCENARIO_COMPLIANCE, WBP_SCENARIO_RUN
from backend.app.schemas.scenario import StrategyView
from backend.app.services.trading_rules_policy import (
    DEFAULT_APPROVED_CRYPTO_BASES,
    approved_crypto_bases_from_rules,
    effective_max_single_weight,
    effective_min_cash_weight,
    trading_rules_fold_notes,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Defaults only — live universe comes from trading_rules PORTFOLIO
# ``approved_crypto_universe`` (see trading_rules_policy).
APPROVED_CRYPTO_BASES: frozenset[str] = frozenset(DEFAULT_APPROVED_CRYPTO_BASES)
APPROVED_CRYPTO_TICKERS: frozenset[str] = frozenset(
    {f"{b}-USD" for b in APPROVED_CRYPTO_BASES}
)

CASH_TICKER_ALIASES: frozenset[str] = frozenset({"CASH", "CASH_USD", "USD"})

TRADING_DAYS_PER_YEAR: int = 252
DEFAULT_RISK_AVERSION: float = 2.5
DEFAULT_TAU: float = 0.05
CASH_COV_EPSILON: float = 1e-12
MIN_VIEW_OMEGA: float = 1e-6
MAX_VIEW_OMEGA: float = 1e2

# Annualized absolute-return tilts added to the prior for qualitative views.
# Keys are normalized tokens found in ``final_view`` / ``view`` text.
QUALITATIVE_VIEW_TILTS: dict[str, float] = {
    "strongly_bullish": 0.08,
    "very_bullish": 0.08,
    "bullish": 0.04,
    "overweight": 0.03,
    "neutral_to_moderately_positive": 0.015,
    "moderately_positive": 0.015,
    "positive": 0.02,
    "neutral": 0.0,
    "hold": 0.0,
    "mixed": 0.0,
    "neutral_to_cautious": -0.01,
    "cautious": -0.015,
    "negative": -0.02,
    "underweight": -0.03,
    "bearish": -0.04,
    "very_bearish": -0.08,
    "strongly_bearish": -0.08,
}

RULE_PORTFOLIO = "PORTFOLIO"
RULE_TRADE = "TRADE"
RULE_CLIENT = "CLIENT"
RULE_TECHNICAL = "TECHNICAL"
# Legacy aliases still accepted when reading trading_rules rows.
_RULE_CATEGORY_ALIASES: dict[str, str] = {
    "POSITION_LIMIT": RULE_PORTFOLIO,
    "CONCENTRATION": RULE_PORTFOLIO,
    "WEIGHT": RULE_PORTFOLIO,
    "TRADING_RESTRICTION": RULE_TRADE,
    "CLIENT_RESTRICTION": RULE_CLIENT,
    "TECHNICAL": RULE_TECHNICAL,
    "PORTFOLIO": RULE_PORTFOLIO,
    "TRADE": RULE_TRADE,
    "CLIENT": RULE_CLIENT,
}


def normalize_rule_category(raw: str) -> str:
    """Map legacy / free-text categories onto the fixed taxonomy."""
    key = str(raw or "").strip().upper().replace(" ", "_")
    return _RULE_CATEGORY_ALIASES.get(key, key)

TIGHTEN_CRYPTO_FACTOR: float = 0.8
TIGHTEN_SINGLE_FACTOR: float = 0.85
TIGHTEN_CASH_STEP: float = 0.01


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ScenarioOptimizerError(Exception):
    """Structured optimizer failure (no bare except in this module)."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = WBP_SCENARIO_RUN,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "message": self.message,
            "details": self.details,
        }


class ComplianceUnresolvedError(ScenarioOptimizerError):
    """Raised when the compliance re-solve loop exhausts without a feasible book."""

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            error_code=WBP_SCENARIO_COMPLIANCE,
            details=details,
        )


# ---------------------------------------------------------------------------
# Constraint / result shapes
# ---------------------------------------------------------------------------


@dataclass
class OptimizeConstraints:
    """Long-only MV constraints (weights are fractions summing to 1)."""

    min_cash_weight: float | None = None
    max_single_weight: float | None = None  # any non-cash name; None = no global cap
    max_crypto_weight: float | None = None
    max_crypto_single_weight: float | None = None  # per CRYPTO name (POSITION_LIMIT)
    restricted_tickers: set[str] = field(default_factory=set)
    cash_tickers: set[str] = field(default_factory=set)
    crypto_tickers: set[str] = field(default_factory=set)
    risk_aversion: float = DEFAULT_RISK_AVERSION
    # Equality locks (fraction of portfolio). Freeze list + advisor overrides.
    fixed_weights: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ComplianceViolation:
    rule_category: str
    rule_name: str
    message: str
    parameters: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ledoit_wolf_cov(returns: pd.DataFrame) -> np.ndarray:
    """Ledoit–Wolf shrunk covariance of columns; cash rows/cols near zero.

    Parameters
    ----------
    returns:
        Period returns with ticker columns. Must have ≥ 2 rows and ≥ 1 column.

    Returns
    -------
    Annualized covariance matrix (n×n) aligned to ``returns.columns``.
    """
    _validate_returns_frame(returns)
    clean = returns.astype(float).replace([np.inf, -np.inf], np.nan).dropna(how="any")
    if clean.shape[0] < 2:
        raise ScenarioOptimizerError(
            "ledoit_wolf_cov requires at least 2 non-NaN return rows",
            details={"rows_after_dropna": int(clean.shape[0])},
        )

    tickers = [str(c) for c in clean.columns]
    X = clean.to_numpy(dtype=float)
    n_features = X.shape[1]

    if n_features == 1:
        period_var = float(np.var(X[:, 0], ddof=1)) if X.shape[0] > 1 else 0.0
        cov_period = np.array([[period_var]], dtype=float)
    else:
        lw = LedoitWolf().fit(X)
        cov_period = np.asarray(lw.covariance_, dtype=float)

    cov = cov_period * float(TRADING_DAYS_PER_YEAR)
    cov = _zero_cash_covariance(cov, tickers)
    # Mild diagonal jitter helps cvxpy when several series are near-collinear.
    jitter = max(CASH_COV_EPSILON, 1e-8 * float(np.trace(cov) / max(len(tickers), 1)))
    cov = cov + np.eye(len(tickers), dtype=float) * jitter
    cov = _zero_cash_covariance(cov, tickers)
    cov = _ensure_symmetric_psd(cov)
    return cov


def black_litterman_returns(
    prior_returns: np.ndarray | pd.Series,
    views_dict: Mapping[str, float],
    confidences: Mapping[str, float],
    cov: np.ndarray,
    *,
    tickers: Sequence[str] | None = None,
    tau: float = DEFAULT_TAU,
) -> np.ndarray:
    """Simple absolute-view Black–Litterman posterior expected returns.

    Parameters
    ----------
    prior_returns:
        Prior mean vector π (annualized), length n. If a Series, index is tickers.
    views_dict:
        Absolute expected returns Q for selected tickers (annualized).
    confidences:
        Per-view confidence in [0, 1]; higher → tighter Ω diagonal.
    cov:
        Covariance Σ (annualized), shape (n, n).
    tickers:
        Required when ``prior_returns`` is an ndarray; otherwise taken from Series index.
    tau:
        Scalar uncertainty on the prior (classic BL).

    Returns
    -------
    Posterior mean vector μ_BL, shape (n,).
    """
    pi, names = _as_aligned_vector(prior_returns, tickers=tickers, name="prior_returns")
    sigma = np.asarray(cov, dtype=float)
    if sigma.shape != (len(names), len(names)):
        raise ScenarioOptimizerError(
            "cov shape does not match prior_returns length",
            details={"cov_shape": list(sigma.shape), "n": len(names)},
        )
    if tau <= 0:
        raise ScenarioOptimizerError("tau must be positive", details={"tau": tau})

    if not views_dict:
        return pi.copy()

    view_names: list[str] = []
    q_list: list[float] = []
    omega_diag: list[float] = []
    name_to_idx = {t: i for i, t in enumerate(names)}

    for raw_ticker, q_val in views_dict.items():
        ticker = _normalize_ticker(str(raw_ticker))
        if ticker not in name_to_idx:
            continue
        conf = float(confidences.get(raw_ticker, confidences.get(ticker, 0.5)))
        conf = min(1.0, max(0.0, conf))
        # Map confidence → view variance: conf=1 → MIN, conf=0 → MAX.
        omega_i = MAX_VIEW_OMEGA * (1.0 - conf) + MIN_VIEW_OMEGA * conf
        # Blend with prior uncertainty scale τ P Σ P' for numerical sanity.
        idx = name_to_idx[ticker]
        prior_scale = float(tau * sigma[idx, idx])
        omega_i = max(omega_i, prior_scale * (1.0 - conf) + MIN_VIEW_OMEGA)
        view_names.append(ticker)
        q_list.append(float(q_val))
        omega_diag.append(omega_i)

    if not view_names:
        return pi.copy()

    k = len(view_names)
    P = np.zeros((k, len(names)), dtype=float)
    for row, ticker in enumerate(view_names):
        P[row, name_to_idx[ticker]] = 1.0
    Q = np.asarray(q_list, dtype=float)
    Omega = np.diag(np.asarray(omega_diag, dtype=float))

    try:
        tau_sigma_inv = np.linalg.inv(tau * sigma)
        omega_inv = np.linalg.inv(Omega)
    except np.linalg.LinAlgError as exc:
        raise ScenarioOptimizerError(
            "Black–Litterman matrix inversion failed",
            details={"reason": str(exc)},
        ) from exc

    # μ = [(τΣ)^{-1} + P' Ω^{-1} P]^{-1} [(τΣ)^{-1} π + P' Ω^{-1} Q]
    middle = tau_sigma_inv + P.T @ omega_inv @ P
    rhs = tau_sigma_inv @ pi + P.T @ omega_inv @ Q
    try:
        mu_bl = np.linalg.solve(middle, rhs)
    except np.linalg.LinAlgError as exc:
        raise ScenarioOptimizerError(
            "Black–Litterman posterior solve failed",
            details={"reason": str(exc)},
        ) from exc
    return np.asarray(mu_bl, dtype=float)


def optimize_weights(
    *,
    expected_returns: np.ndarray | pd.Series,
    cov: np.ndarray,
    constraints: OptimizeConstraints | Mapping[str, Any],
    tickers: Sequence[str] | None = None,
) -> dict[str, float]:
    """Mean-variance long-only weights via cvxpy.

    Constraints enforced when present on ``constraints``:
    - weights sum to 1
    - long-only (w ≥ 0)
    - min cash weight (sum over cash tickers)
    - max single-name weight
    - max total CRYPTO weight
    - zero weight for restricted tickers
    """
    mu, names = _as_aligned_vector(
        expected_returns, tickers=tickers, name="expected_returns"
    )
    sigma = np.asarray(cov, dtype=float)
    if sigma.shape != (len(names), len(names)):
        raise ScenarioOptimizerError(
            "cov shape does not match expected_returns length",
            details={"cov_shape": list(sigma.shape), "n": len(names)},
        )

    cons = _coerce_constraints(constraints, names)
    n = len(names)
    w = cp.Variable(n)
    risk_aversion = float(cons.risk_aversion)
    if risk_aversion <= 0:
        raise ScenarioOptimizerError(
            "risk_aversion must be positive",
            details={"risk_aversion": risk_aversion},
        )

    objective = cp.Maximize(mu @ w - 0.5 * risk_aversion * cp.quad_form(w, _psd_cov(sigma)))
    problem_constraints: list[cp.Constraint] = [
        cp.sum(w) == 1.0,
        w >= 0.0,
    ]

    name_to_idx = {t: i for i, t in enumerate(names)}
    cash_idx = [name_to_idx[t] for t in cons.cash_tickers if t in name_to_idx]
    crypto_idx = [name_to_idx[t] for t in cons.crypto_tickers if t in name_to_idx]
    non_cash_idx = [i for i, t in enumerate(names) if i not in set(cash_idx)]

    if cons.max_single_weight is not None and non_cash_idx:
        problem_constraints.append(w[non_cash_idx] <= float(cons.max_single_weight))

    if cons.max_crypto_single_weight is not None and crypto_idx:
        problem_constraints.append(
            w[crypto_idx] <= float(cons.max_crypto_single_weight)
        )

    for ticker in cons.restricted_tickers:
        if ticker in name_to_idx and ticker not in cons.fixed_weights:
            problem_constraints.append(w[name_to_idx[ticker]] == 0.0)

    fixed_sum = 0.0
    for ticker, target in cons.fixed_weights.items():
        nt = _normalize_ticker(ticker)
        if nt not in name_to_idx:
            raise ScenarioOptimizerError(
                f"fixed weight for unknown ticker {nt}",
                details={"ticker": nt, "fixed_weights": dict(cons.fixed_weights)},
            )
        wt = float(target)
        if not np.isfinite(wt) or wt < -1e-12 or wt > 1.0 + 1e-12:
            raise ScenarioOptimizerError(
                f"fixed weight for {nt} must be in [0, 1]",
                details={"ticker": nt, "weight": wt},
            )
        wt = float(min(1.0, max(0.0, wt)))
        problem_constraints.append(w[name_to_idx[nt]] == wt)
        fixed_sum += wt
    if fixed_sum > 1.0 + 1e-9:
        raise ScenarioOptimizerError(
            "sum of fixed_weights exceeds 1.0",
            details={"fixed_sum": fixed_sum, "fixed_weights": dict(cons.fixed_weights)},
        )

    if cons.min_cash_weight is not None and cash_idx:
        problem_constraints.append(
            cp.sum(w[cash_idx]) >= float(cons.min_cash_weight)
        )
    elif cons.min_cash_weight is not None and not cash_idx:
        raise ScenarioOptimizerError(
            "min_cash_weight set but no cash tickers in universe",
            details={"min_cash_weight": cons.min_cash_weight, "tickers": names},
        )

    if cons.max_crypto_weight is not None and crypto_idx:
        problem_constraints.append(
            cp.sum(w[crypto_idx]) <= float(cons.max_crypto_weight)
        )

    problem = cp.Problem(objective, problem_constraints)
    _solve_cvxpy(problem)

    if w.value is None:
        raise ScenarioOptimizerError(
            "optimizer returned no solution",
            details={"status": problem.status},
        )

    raw = np.asarray(w.value, dtype=float).reshape(-1)
    raw = np.clip(raw, 0.0, None)
    # Force restricted exactly to zero post-solve (numerical drift).
    for ticker in cons.restricted_tickers:
        if ticker in name_to_idx:
            raw[name_to_idx[ticker]] = 0.0
    total = float(raw.sum())
    if total <= 0:
        raise ScenarioOptimizerError(
            "optimizer produced non-positive total weight",
            details={"weights": raw.tolist()},
        )
    raw = raw / total
    return {names[i]: float(raw[i]) for i in range(n) if raw[i] > 1e-10 or names[i] in cons.cash_tickers}


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


# ---------------------------------------------------------------------------
# Universe / views / rules helpers (public for unit tests / pipeline wiring)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_returns_frame(returns: pd.DataFrame) -> None:
    if not isinstance(returns, pd.DataFrame):
        raise ScenarioOptimizerError(
            "returns must be a pandas DataFrame",
            details={"type": type(returns).__name__},
        )
    if returns.shape[0] < 1 or returns.shape[1] < 1:
        raise ScenarioOptimizerError(
            "returns DataFrame is empty",
            details={"shape": list(returns.shape)},
        )


def _normalize_ticker(raw: str) -> str:
    """Optimizer cash form keeps ``CASH_USD``; crypto/equity via shared helper."""
    from backend.app.services.ticker_normalize import normalize_ticker

    t = str(raw or "").strip().upper()
    if t in CASH_TICKER_ALIASES:
        if t == "USD":
            return "USD"
        return "CASH_USD" if t in {"CASH", "CASH_USD"} else t
    return normalize_ticker(t)


def _normalize_asset_class(raw: str) -> str:
    ac = raw.strip().upper()
    if ac in {"COINS", "COIN"}:
        return "CRYPTO"
    if ac in {"EQUITY", "CASH", "CRYPTO"}:
        return ac
    return "EQUITY"


def _is_cash_ticker(ticker: str) -> bool:
    t = ticker.strip().upper()
    return t in CASH_TICKER_ALIASES or t in {"CASH", "CASH_USD", "USD"}


def _is_approved_crypto(ticker: str) -> bool:
    t = _normalize_ticker(ticker)
    if t in APPROVED_CRYPTO_TICKERS:
        return True
    base = t[:-4] if t.endswith("-USD") else t
    return base in APPROVED_CRYPTO_BASES


def _client_rule_applies(params: Mapping[str, Any], client_alias: str | None) -> bool:
    if client_alias is None:
        # Firm-wide application when no alias filter context: apply if no alias
        # keyed on the rule, otherwise skip client-specific rows.
        return not (params.get("client_alias") or params.get("client_id"))
    alias = str(client_alias).strip().lower()
    rule_alias = str(params.get("client_alias") or "").strip().lower()
    if rule_alias and rule_alias == alias:
        return True
    # client_id match left to caller identity; alias is Phase-1 primary key.
    if rule_alias and rule_alias != alias:
        return False
    return not rule_alias


def _as_aligned_vector(
    values: np.ndarray | pd.Series,
    *,
    tickers: Sequence[str] | None,
    name: str,
) -> tuple[np.ndarray, list[str]]:
    if isinstance(values, pd.Series):
        names = [_normalize_ticker(str(i)) for i in values.index]
        vec = values.astype(float).to_numpy()
        return np.asarray(vec, dtype=float), names
    arr = np.asarray(values, dtype=float).reshape(-1)
    if tickers is None:
        raise ScenarioOptimizerError(
            f"{name} ndarray requires tickers=",
            details={"length": int(arr.shape[0])},
        )
    names = [_normalize_ticker(str(t)) for t in tickers]
    if len(names) != arr.shape[0]:
        raise ScenarioOptimizerError(
            f"{name} length does not match tickers",
            details={"n_values": int(arr.shape[0]), "n_tickers": len(names)},
        )
    return arr, names


def _historical_mean_returns(returns: pd.DataFrame) -> np.ndarray:
    clean = returns.astype(float).replace([np.inf, -np.inf], np.nan)
    means = clean.mean(axis=0, skipna=True).fillna(0.0).to_numpy(dtype=float)
    return means * float(TRADING_DAYS_PER_YEAR)


def _align_returns_to_universe(
    returns: pd.DataFrame, tickers: Sequence[str]
) -> pd.DataFrame:
    """Align columns to universe; synthesize missing non-cash series.

    Missing approved-crypto (or other) columns without history get small
    independent noise so Ledoit–Wolf / cvxpy stay well-conditioned. Cash is
    exact zeros. This is a Phase-1 UAT stand-in — callers should pass real
    history when available.
    """
    cols: dict[str, pd.Series] = {}
    idx = returns.index
    n = len(idx)
    # Deterministic synthetic residuals for missing names (UAT only).
    rng = np.random.default_rng(20260918)
    for t in tickers:
        if t in returns.columns:
            cols[t] = returns[t].astype(float)
        elif _is_cash_ticker(t):
            cols[t] = pd.Series(0.0, index=idx, dtype=float)
        else:
            # ~40% ann. vol idiosyncratic stand-in when history is absent.
            daily_vol = 0.40 / np.sqrt(float(TRADING_DAYS_PER_YEAR))
            noise = rng.normal(0.0, daily_vol, size=n)
            cols[t] = pd.Series(noise, index=idx, dtype=float)
    return pd.DataFrame(cols, index=idx)


def _zero_cash_covariance(cov: np.ndarray, tickers: Sequence[str]) -> np.ndarray:
    out = cov.copy()
    for i, t in enumerate(tickers):
        if not _is_cash_ticker(t):
            continue
        out[i, :] = 0.0
        out[:, i] = 0.0
        out[i, i] = CASH_COV_EPSILON
    return out


def _psd_cov(cov: np.ndarray) -> cp.Expression:
    """Wrap covariance so cvxpy treats quad_form as DCP-compliant PSD."""
    return cp.psd_wrap(np.asarray(cov, dtype=float))


def _ensure_symmetric_psd(cov: np.ndarray) -> np.ndarray:
    mat = 0.5 * (cov + cov.T)
    # Eigenvalue clip for numerical PSD (cvxpy quad_form).
    try:
        eigvals, eigvecs = np.linalg.eigh(mat)
    except np.linalg.LinAlgError as exc:
        raise ScenarioOptimizerError(
            "covariance eigendecomposition failed",
            details={"reason": str(exc)},
        ) from exc
    eigvals = np.clip(eigvals, CASH_COV_EPSILON, None)
    return (eigvecs * eigvals) @ eigvecs.T


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


def _solve_cvxpy(problem: cp.Problem) -> None:
    """Try installed QP/SOCP solvers; accept OPTIMAL_INACCURATE for UAT."""
    solvers: list[Any] = [cp.OSQP, cp.SCS, cp.ECOS]
    if hasattr(cp, "CLARABEL"):
        solvers.insert(2, cp.CLARABEL)
    solvers.append(None)  # cvxpy default last

    last_status = "unknown"
    last_error: Exception | None = None
    for solver in solvers:
        try:
            if solver is None:
                problem.solve(warm_start=True, verbose=False)
            elif solver == cp.OSQP:
                problem.solve(
                    solver=solver,
                    warm_start=True,
                    verbose=False,
                    eps_abs=1e-6,
                    eps_rel=1e-6,
                    max_iter=20000,
                )
            elif solver == cp.SCS:
                problem.solve(
                    solver=solver,
                    warm_start=True,
                    verbose=False,
                    eps=1e-5,
                    max_iters=10000,
                )
            else:
                problem.solve(solver=solver, warm_start=True, verbose=False)
            last_status = str(problem.status)
            if problem.status in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE):
                return
        except (cp.SolverError, ValueError) as exc:
            last_error = exc
            last_status = "solver_error"
            continue
    detail: dict[str, Any] = {"status": last_status}
    if last_error is not None:
        detail["solver_error"] = str(last_error)
    raise ScenarioOptimizerError(
        "mean-variance optimization failed or infeasible",
        details=detail,
    )


__all__ = [
    "APPROVED_CRYPTO_BASES",
    "APPROVED_CRYPTO_TICKERS",
    "CASH_TICKER_ALIASES",
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
