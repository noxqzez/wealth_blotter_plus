"""Rule-based safety gate for WF-02 quant / optimizer inputs and outputs.

Validates mathematical sanity (correlations, PSD covariance, finite weights,
feasible bounds, no missing assets, etc.). On failure:

* logs ``quant_safety_gate_failed`` for tech investigation (structured details)
* returns corrective feedback for the prior step (strategist views / constraints)
  so ``propose_opti_folio`` can self-heal up to ``MAX_SAFETY_RETRIES`` times
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import structlog

from backend.app.core.error_codes import WBP_SCENARIO_QUANT_SAFETY
from backend.app.services.scenario_optimizer import (
    OptimizeConstraints,
    ScenarioOptimizerError,
)

logger = structlog.get_logger(__name__)

MAX_SAFETY_RETRIES: int = 3
_WEIGHT_SUM_TOL = 1e-4
_CORR_TOL = 1e-6
_EIG_TOL = -1e-8


@dataclass
class SafetyFinding:
    code: str
    message: str
    severity: str = "error"  # error | warning
    field: str | None = None


@dataclass
class SafetyReport:
    ok: bool
    findings: list[SafetyFinding] = field(default_factory=list)
    feedback_for_prior: list[str] = field(default_factory=list)
    corrections_applied: list[str] = field(default_factory=list)

    def error_messages(self) -> list[str]:
        return [f.message for f in self.findings if f.severity == "error"]


def validate_returns_matrix(returns: pd.DataFrame) -> SafetyReport:
    findings: list[SafetyFinding] = []
    feedback: list[str] = []
    if returns is None or not isinstance(returns, pd.DataFrame):
        findings.append(
            SafetyFinding("RETURNS_TYPE", "Returns must be a pandas DataFrame")
        )
        return SafetyReport(ok=False, findings=findings, feedback_for_prior=feedback)

    if returns.empty or returns.shape[0] < 2 or returns.shape[1] < 1:
        findings.append(
            SafetyFinding(
                "RETURNS_EMPTY",
                f"Insufficient return history shape={tuple(returns.shape)}",
            )
        )
        feedback.append("Prior step: widen lookback or drop assets with empty history.")
        return SafetyReport(ok=False, findings=findings, feedback_for_prior=feedback)

    arr = returns.to_numpy(dtype=float)
    if not np.isfinite(arr).all():
        n_bad = int(np.size(arr) - np.isfinite(arr).sum())
        findings.append(
            SafetyFinding(
                "RETURNS_NONFINITE",
                f"Returns contain {n_bad} blank/NaN/Inf value(s)",
                field="returns",
            )
        )
        feedback.append(
            "Prior step: drop tickers with incomplete history or fill gaps before optimize."
        )

    return SafetyReport(
        ok=not any(f.severity == "error" for f in findings),
        findings=findings,
        feedback_for_prior=feedback,
    )


def validate_covariance(
    cov: np.ndarray,
    *,
    tickers: Sequence[str],
) -> SafetyReport:
    findings: list[SafetyFinding] = []
    feedback: list[str] = []
    sigma = np.asarray(cov, dtype=float)
    n = len(tickers)

    if sigma.ndim != 2 or sigma.shape != (n, n):
        findings.append(
            SafetyFinding(
                "COV_SHAPE",
                f"Covariance shape {sigma.shape} != ({n},{n})",
                field="cov",
            )
        )
        feedback.append("Rebuild covariance on the aligned universe tickers.")
        return SafetyReport(ok=False, findings=findings, feedback_for_prior=feedback)

    if not np.isfinite(sigma).all():
        findings.append(
            SafetyFinding("COV_NONFINITE", "Covariance has blank/NaN/Inf entries")
        )
        feedback.append("Re-estimate covariance after cleaning returns.")

    if not np.allclose(sigma, sigma.T, atol=1e-8):
        findings.append(
            SafetyFinding("COV_ASYMMETRIC", "Covariance matrix is not symmetric")
        )
        feedback.append("Symmetrize covariance (0.5*(Σ+Σᵀ)) before optimize.")

    try:
        eigvals = np.linalg.eigvalsh(0.5 * (sigma + sigma.T))
    except np.linalg.LinAlgError:
        findings.append(
            SafetyFinding("COV_EIG_FAILED", "Covariance eigendecomposition failed")
        )
        feedback.append("Add diagonal jitter / drop collinear assets.")
        return SafetyReport(ok=False, findings=findings, feedback_for_prior=feedback)

    if float(np.min(eigvals)) < _EIG_TOL:
        findings.append(
            SafetyFinding(
                "COV_NOT_PSD",
                f"Covariance not PSD (min eigenvalue={float(np.min(eigvals)):.3e})",
                field="cov",
            )
        )
        feedback.append(
            "Prior/quant: project covariance to PSD or increase Ledoit–Wolf jitter."
        )

    # Correlation bounds from cov
    d = np.sqrt(np.clip(np.diag(sigma), 0.0, None))
    with np.errstate(divide="ignore", invalid="ignore"):
        denom = np.outer(d, d)
        corr = np.divide(sigma, denom, out=np.zeros_like(sigma), where=denom > 0)
    np.fill_diagonal(corr, 1.0)
    if np.nanmax(corr) > 1.0 + _CORR_TOL or np.nanmin(corr) < -1.0 - _CORR_TOL:
        findings.append(
            SafetyFinding(
                "CORR_OOB",
                f"Implied correlations outside [-1,1] "
                f"(min={float(np.nanmin(corr)):.4f}, max={float(np.nanmax(corr)):.4f})",
                field="correlation",
            )
        )
        feedback.append("Repair covariance diagonals before forming correlations.")

    return SafetyReport(
        ok=not any(f.severity == "error" for f in findings),
        findings=findings,
        feedback_for_prior=feedback,
    )


def validate_constraints(
    constraints: OptimizeConstraints,
    *,
    tickers: Sequence[str],
) -> SafetyReport:
    findings: list[SafetyFinding] = []
    feedback: list[str] = []
    cons = constraints

    pairs: list[tuple[str, float | None, float | None]] = [
        ("cash", cons.min_cash_weight, None),
        ("single", None, cons.max_single_weight),
        ("crypto_total", None, cons.max_crypto_weight),
        ("crypto_single", None, cons.max_crypto_single_weight),
    ]
    for name, lo, hi in pairs:
        if lo is not None and (not np.isfinite(lo) or lo < 0 or lo > 1):
            findings.append(
                SafetyFinding(
                    "BOUND_LO_OOB",
                    f"min_{name}={lo} not in [0,1]",
                    field=name,
                )
            )
            feedback.append(f"Clamp or clear invalid min bound for {name}.")
        if hi is not None and (not np.isfinite(hi) or hi < 0 or hi > 1):
            findings.append(
                SafetyFinding(
                    "BOUND_HI_OOB",
                    f"max_{name}={hi} not in [0,1]",
                    field=name,
                )
            )
            feedback.append(f"Clamp or clear invalid max bound for {name}.")

    # Impossible: min cash > 1, or min cash > 1 - impossible with other mins
    if cons.min_cash_weight is not None and cons.max_single_weight is not None:
        if cons.min_cash_weight > 1.0 + 1e-12:
            findings.append(
                SafetyFinding(
                    "BOUND_INFEASIBLE",
                    f"min_cash_weight={cons.min_cash_weight} > 1",
                )
            )
            feedback.append("Lower min_cash_weight to ≤ 1.")

    # Explicit min > max style: if both cash floor and a "max cash" existed we'd check;
    # treat max_crypto_single > max_crypto_total as contradictory.
    if (
        cons.max_crypto_single_weight is not None
        and cons.max_crypto_weight is not None
        and cons.max_crypto_single_weight > cons.max_crypto_weight + 1e-12
    ):
        findings.append(
            SafetyFinding(
                "BOUND_MIN_GT_MAX",
                "max_crypto_single_weight > max_crypto_weight (contradictory caps)",
                field="crypto",
            )
        )
        feedback.append(
            "Prior/compliance: set max_crypto_single ≤ max_crypto_total."
        )

    if cons.risk_aversion is not None and (
        not np.isfinite(cons.risk_aversion) or cons.risk_aversion <= 0
    ):
        findings.append(
            SafetyFinding(
                "RISK_AVERSION",
                f"risk_aversion={cons.risk_aversion} must be finite and > 0",
            )
        )
        feedback.append("Reset risk_aversion to default (2.5).")

    unknown_restricted = sorted(
        t for t in cons.restricted_tickers if t and t not in set(tickers)
    )
    if unknown_restricted:
        findings.append(
            SafetyFinding(
                "RESTRICTED_MISSING",
                f"Restricted tickers not in universe: {unknown_restricted}",
                severity="warning",
            )
        )

    return SafetyReport(
        ok=not any(f.severity == "error" for f in findings),
        findings=findings,
        feedback_for_prior=feedback,
    )


def validate_optimizer_inputs(
    *,
    returns: pd.DataFrame,
    cov: np.ndarray,
    tickers: Sequence[str],
    constraints: OptimizeConstraints,
    prices: Mapping[str, float] | None,
    portfolio_value: float | None,
    strategy_views: Mapping[str, float] | None = None,
) -> SafetyReport:
    reports = [
        validate_returns_matrix(returns),
        validate_covariance(cov, tickers=tickers),
        validate_constraints(constraints, tickers=tickers),
    ]
    findings: list[SafetyFinding] = []
    feedback: list[str] = []
    for r in reports:
        findings.extend(r.findings)
        feedback.extend(r.feedback_for_prior)

    ticker_set = {str(t) for t in tickers}
    if prices:
        for t, px in prices.items():
            if t not in ticker_set:
                continue
            if px is None or not np.isfinite(float(px)) or float(px) <= 0:
                findings.append(
                    SafetyFinding(
                        "PRICE_INVALID",
                        f"Invalid local last price for {t}: {px}",
                        field=str(t),
                    )
                )
                feedback.append(f"Prior/market: refresh price for {t}.")

    if portfolio_value is not None and (
        not np.isfinite(portfolio_value) or float(portfolio_value) <= 0
    ):
        findings.append(
            SafetyFinding(
                "PORTFOLIO_VALUE",
                f"portfolio_value={portfolio_value} must be finite and > 0",
            )
        )
        feedback.append("Recompute portfolio USD notionals before optimize.")

    if strategy_views:
        for t, v in strategy_views.items():
            if t not in ticker_set:
                findings.append(
                    SafetyFinding(
                        "VIEW_MISSING_ASSET",
                        f"Strategy view for {t} not in optimizer universe",
                        severity="warning",
                        field=str(t),
                    )
                )
                feedback.append(f"Strategist: drop view for out-of-universe asset {t}.")
            if v is None or not np.isfinite(float(v)):
                findings.append(
                    SafetyFinding(
                        "VIEW_NONFINITE",
                        f"Strategy absolute return for {t} is blank/non-finite",
                        field=str(t),
                    )
                )
                feedback.append(f"Strategist: remove or repair view for {t}.")

    ok = not any(f.severity == "error" for f in findings)
    return SafetyReport(ok=ok, findings=findings, feedback_for_prior=feedback)


def validate_optimizer_outputs(
    *,
    weights: Mapping[str, float],
    positions: Sequence[Mapping[str, Any]],
    tickers: Sequence[str],
) -> SafetyReport:
    findings: list[SafetyFinding] = []
    feedback: list[str] = []
    if not weights:
        findings.append(SafetyFinding("WEIGHTS_EMPTY", "Optimizer returned no weights"))
        feedback.append("Quant: re-solve with loosened constraints / prior only.")
        return SafetyReport(ok=False, findings=findings, feedback_for_prior=feedback)

    vals = []
    for t, w in weights.items():
        if w is None or not np.isfinite(float(w)):
            findings.append(
                SafetyFinding("WEIGHT_NONFINITE", f"Non-finite weight for {t}", field=str(t))
            )
            feedback.append(f"Quant: drop/repair weight for {t} and renormalize.")
            continue
        wf = float(w)
        if wf < -1e-8:
            findings.append(
                SafetyFinding("WEIGHT_NEGATIVE", f"Negative weight for {t}: {wf}", field=str(t))
            )
            feedback.append("Quant: enforce long-only (w≥0) and renormalize.")
        vals.append(max(0.0, wf))

    total = float(sum(vals)) if vals else float("nan")
    if not np.isfinite(total) or abs(total - 1.0) > _WEIGHT_SUM_TOL:
        findings.append(
            SafetyFinding(
                "WEIGHT_SUM",
                f"Weights sum to {total} (expected ≈ 1.0 ± {_WEIGHT_SUM_TOL})",
            )
        )
        feedback.append("Quant: renormalize weights to sum to 1.")

    ticker_set = {str(t) for t in tickers}
    for t in weights:
        if str(t) not in ticker_set:
            findings.append(
                SafetyFinding(
                    "WEIGHT_UNKNOWN_ASSET",
                    f"Weight for unknown asset {t}",
                    severity="warning",
                )
            )

    for pos in positions:
        t = str(pos.get("ticker") or "")
        qty = pos.get("quantity")
        notion = pos.get("notional_value")
        w = pos.get("weight_pct")
        if qty is not None and not np.isfinite(float(qty)):
            findings.append(
                SafetyFinding("POS_QTY", f"Non-finite quantity for {t}", field=t)
            )
        if notion is not None and not np.isfinite(float(notion)):
            findings.append(
                SafetyFinding("POS_NOTIONAL", f"Non-finite notional for {t}", field=t)
            )
        if w is not None and not np.isfinite(float(w)):
            findings.append(
                SafetyFinding("POS_WEIGHT", f"Non-finite position weight for {t}", field=t)
            )
        if qty is not None and float(qty) < -1e-9:
            findings.append(
                SafetyFinding("POS_NEG_QTY", f"Negative quantity for {t}", field=t)
            )
            feedback.append("Quant: truncate short quantities; long-only book.")

    ok = not any(f.severity == "error" for f in findings)
    return SafetyReport(ok=ok, findings=findings, feedback_for_prior=feedback)


def apply_input_corrections(
    *,
    returns: pd.DataFrame,
    cov: np.ndarray,
    constraints: OptimizeConstraints,
    views_dict: dict[str, float],
    confidences: dict[str, float],
    report: SafetyReport,
) -> tuple[pd.DataFrame, np.ndarray, OptimizeConstraints, dict[str, float], dict[str, float], list[str]]:
    """Deterministic repairs suggested by the safety gate (prior-step feedback)."""
    corrections: list[str] = []
    clean = returns.replace([np.inf, -np.inf], np.nan).dropna(how="any")
    if clean.shape[0] < returns.shape[0]:
        corrections.append(
            f"Dropped {returns.shape[0] - clean.shape[0]} non-finite return row(s)."
        )
    returns_out = clean if not clean.empty else returns

    sigma = np.asarray(cov, dtype=float)
    if sigma.size and np.isfinite(sigma).all():
        sigma = 0.5 * (sigma + sigma.T)
        try:
            eigvals, eigvecs = np.linalg.eigh(sigma)
            eigvals = np.clip(eigvals, 1e-12, None)
            sigma = eigvecs @ np.diag(eigvals) @ eigvecs.T
            sigma = 0.5 * (sigma + sigma.T)
            corrections.append("Projected covariance to PSD and symmetrized.")
        except np.linalg.LinAlgError:
            corrections.append("Covariance PSD projection failed; left unchanged.")

    cons = OptimizeConstraints(
        min_cash_weight=constraints.min_cash_weight,
        max_single_weight=constraints.max_single_weight,
        max_crypto_weight=constraints.max_crypto_weight,
        max_crypto_single_weight=constraints.max_crypto_single_weight,
        restricted_tickers=set(constraints.restricted_tickers),
        cash_tickers=set(constraints.cash_tickers),
        crypto_tickers=set(constraints.crypto_tickers),
        risk_aversion=constraints.risk_aversion,
        fixed_weights=dict(constraints.fixed_weights),
    )

    def _clamp01(x: float | None) -> float | None:
        if x is None:
            return None
        if not np.isfinite(x):
            return None
        return float(min(1.0, max(0.0, x)))

    cons.min_cash_weight = _clamp01(cons.min_cash_weight)
    cons.max_single_weight = _clamp01(cons.max_single_weight)
    cons.max_crypto_weight = _clamp01(cons.max_crypto_weight)
    cons.max_crypto_single_weight = _clamp01(cons.max_crypto_single_weight)
    if (
        cons.max_crypto_single_weight is not None
        and cons.max_crypto_weight is not None
        and cons.max_crypto_single_weight > cons.max_crypto_weight
    ):
        cons.max_crypto_single_weight = cons.max_crypto_weight
        corrections.append("Capped max_crypto_single to max_crypto_total.")
    if cons.risk_aversion is None or not np.isfinite(cons.risk_aversion) or cons.risk_aversion <= 0:
        cons.risk_aversion = 2.5
        corrections.append("Reset risk_aversion to 2.5.")

    # Feed back to strategist views: drop non-finite / extreme absolute returns
    views_out = dict(views_dict)
    conf_out = dict(confidences)
    dropped = []
    for t, v in list(views_out.items()):
        if v is None or not np.isfinite(float(v)) or abs(float(v)) > 5.0:
            dropped.append(t)
            views_out.pop(t, None)
            conf_out.pop(t, None)
    if dropped:
        corrections.append(
            f"Strategist feedback: dropped invalid absolute views for {dropped}."
        )

    codes = {f.code for f in report.findings}
    if "VIEW_MISSING_ASSET" in codes or "VIEW_NONFINITE" in codes:
        # Already handled by drop above; note feedback consumed
        corrections.append("Applied strategist view cleanup from safety feedback.")

    return returns_out, sigma, cons, views_out, conf_out, corrections


def apply_output_corrections(
    weights: Mapping[str, float],
) -> tuple[dict[str, float], list[str]]:
    """Long-only renormalize finite weights."""
    corrections: list[str] = []
    cleaned: dict[str, float] = {}
    for t, w in weights.items():
        if w is None or not np.isfinite(float(w)):
            corrections.append(f"Dropped non-finite weight for {t}.")
            continue
        wf = float(w)
        if wf < 0:
            corrections.append(f"Clamped negative weight for {t} to 0.")
            wf = 0.0
        cleaned[str(t)] = wf
    total = sum(cleaned.values())
    if total <= 0 or not np.isfinite(total):
        return dict(weights), corrections + ["Cannot renormalize empty/invalid weights."]
    if abs(total - 1.0) > _WEIGHT_SUM_TOL:
        cleaned = {t: v / total for t, v in cleaned.items()}
        corrections.append(f"Renormalized weights (was sum={total:.6f}).")
    return cleaned, corrections


def log_safety_failure(report: SafetyReport, *, stage: str, attempt: int) -> None:
    """Internal tech log — full finding list for investigation."""
    logger.error(
        "quant_safety_gate_failed",
        stage=stage,
        attempt=attempt,
        error_code=WBP_SCENARIO_QUANT_SAFETY,
        findings=[
            {"code": f.code, "severity": f.severity, "message": f.message, "field": f.field}
            for f in report.findings
        ],
        feedback_for_prior=list(report.feedback_for_prior),
    )


def raise_safety_exhausted(report: SafetyReport, *, attempts: int) -> None:
    raise ScenarioOptimizerError(
        "Quant safety gate failed after retries — see logs for tech investigation.",
        error_code=WBP_SCENARIO_QUANT_SAFETY,
        details={
            "attempts": attempts,
            "findings": [f.message for f in report.findings],
            "feedback_for_prior": list(report.feedback_for_prior),
        },
    )
