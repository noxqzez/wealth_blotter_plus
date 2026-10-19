"""Black–Litterman and mean-variance math primitives."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import cvxpy as cp
import numpy as np
import pandas as pd
from sklearn.covariance import LedoitWolf

from backend.app.services.scenario_optimizer_compliance import _coerce_constraints
from backend.app.services.scenario_optimizer_types import (
    CASH_COV_EPSILON,
    DEFAULT_RISK_AVERSION,
    DEFAULT_TAU,
    MAX_VIEW_OMEGA,
    MIN_VIEW_OMEGA,
    TRADING_DAYS_PER_YEAR,
    OptimizeConstraints,
    ScenarioOptimizerError,
    _is_cash_ticker,
    _normalize_ticker,
)

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
