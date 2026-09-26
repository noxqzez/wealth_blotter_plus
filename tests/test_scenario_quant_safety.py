"""Tests for quant / optimizer rule-based safety gate."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.app.services.scenario_optimizer import OptimizeConstraints
from backend.app.services.scenario_quant_safety import (
    apply_input_corrections,
    apply_output_corrections,
    validate_constraints,
    validate_covariance,
    validate_optimizer_outputs,
    validate_returns_matrix,
)


def test_returns_rejects_nan() -> None:
    df = pd.DataFrame({"A": [0.01, np.nan, 0.02], "B": [0.0, 0.01, 0.01]})
    report = validate_returns_matrix(df)
    assert report.ok is False
    assert any(f.code == "RETURNS_NONFINITE" for f in report.findings)
    assert report.feedback_for_prior


def test_corr_out_of_bounds_detected() -> None:
    # Ill-conditioned cov → bad correlations
    cov = np.array([[1.0, 2.0], [2.0, 1.0]], dtype=float)
    report = validate_covariance(cov, tickers=["A", "B"])
    assert report.ok is False
    codes = {f.code for f in report.findings}
    assert "CORR_OOB" in codes or "COV_NOT_PSD" in codes


def test_min_gt_max_crypto_caps() -> None:
    cons = OptimizeConstraints(
        max_crypto_weight=0.05,
        max_crypto_single_weight=0.10,
    )
    report = validate_constraints(cons, tickers=["BTC-USD", "ETH-USD"])
    assert report.ok is False
    assert any(f.code == "BOUND_MIN_GT_MAX" for f in report.findings)
    _, _, fixed, _, _, corrections = apply_input_corrections(
        returns=pd.DataFrame({"BTC-USD": [0.01, 0.02], "ETH-USD": [0.0, 0.01]}),
        cov=np.eye(2),
        constraints=cons,
        views_dict={},
        confidences={},
        report=report,
    )
    assert fixed.max_crypto_single_weight <= fixed.max_crypto_weight  # type: ignore[operator]
    assert corrections


def test_output_renormalize_and_clamp() -> None:
    weights = {"A": 0.6, "B": -0.1, "C": float("nan")}
    report = validate_optimizer_outputs(
        weights=weights, positions=[], tickers=["A", "B", "C"]
    )
    assert report.ok is False
    cleaned, corr = apply_output_corrections(weights)
    assert cleaned["B"] == 0.0
    assert "C" not in cleaned
    assert abs(sum(cleaned.values()) - 1.0) < 1e-8
    assert corr
