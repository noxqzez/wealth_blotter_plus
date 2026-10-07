"""Realistic mock datasets when Supabase is unavailable."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pandas as pd

_NOW = datetime.now(timezone.utc)


def _d(days_ago: int, hour: int = 12) -> datetime:
    return (_NOW - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


ADVISOR_IDS: list[str] = [f"adv-{i:02d}" for i in range(1, 31)]
ADVISOR_LABELS: dict[str, str] = {aid: f"Advisor {aid[-2:]}" for aid in ADVISOR_IDS}

# Marketing / training narratives for Activation Rate (active ÷ onboarded).
LOGIN_SCENARIOS: dict[str, dict[str, Any]] = {
    "Pre-SSO (password / fragmented)": {
        "active_share": 0.30,
        "run_scale": 0.45,
        "commercial_mult": 0.75,
        "open_mult": 0.90,
        "blurb": "Fragmented logins — many onboarded, few return weekly.",
    },
    "Post-SSO (firm IdP)": {
        "active_share": 0.82,
        "run_scale": 1.0,
        "commercial_mult": 1.0,
        "open_mult": 1.0,
        "blurb": "Firm SSO — frictionless access lifts activation and proposal throughput.",
    },
    "BYOK (advisor API keys)": {
        "active_share": 0.55,
        "run_scale": 0.70,
        "commercial_mult": 1.35,
        "open_mult": 0.55,
        "blurb": "Bring-your-own-key — mid activation; spend skews commercial.",
    },
}
DEFAULT_LOGIN_SCENARIO = "Post-SSO (firm IdP)"


def mock_bundle(
    *,
    days: int = 30,
    login_scenario: str = DEFAULT_LOGIN_SCENARIO,
) -> dict[str, Any]:
    """Return typed-ish dict of DataFrames / summaries for the exec monitor."""
    scenario = LOGIN_SCENARIOS.get(login_scenario) or LOGIN_SCENARIOS[DEFAULT_LOGIN_SCENARIO]
    n_active = max(1, int(round(len(ADVISOR_IDS) * float(scenario["active_share"]))))
    active_ids = ADVISOR_IDS[:n_active]

    runs = _scenario_runs(days, active_ids=active_ids, run_scale=float(scenario["run_scale"]))
    trades = _staged_trades(runs)
    ratings = _ratings(runs)
    diffs = _param_diffs(runs)
    portfolios = _portfolios()
    rules = _trading_rules()
    token_daily = _token_usage(
        days,
        commercial_mult=float(scenario["commercial_mult"]),
        open_mult=float(scenario["open_mult"]),
    )
    return {
        "source": "mock",
        "as_of": _NOW.isoformat(),
        "login_scenario": login_scenario,
        "login_scenario_blurb": scenario["blurb"],
        "advisors": ADVISOR_LABELS,
        "scenario_runs": runs,
        "staged_trades": trades,
        "interaction_ratings": ratings,
        "scenario_param_diffs": diffs,
        "portfolios": portfolios,
        "trading_rules": rules,
        "token_daily": token_daily,
        "configs": {
            "business_guardrails": {
                "coin_temperature": 2,
                "confidence_threshold": 0.85,
            },
            "technical_limits": {"version": 1},
        },
        "system_status": {
            "supabase": "Mock mode",
            "rules_engine": "Active",
            "agent_engine": "Healthy",
        },
    }


def _scenario_runs(
    days: int,
    *,
    active_ids: list[str],
    run_scale: float = 1.0,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    phases = [
        "import",
        "base_folio",
        "scenario_parse",
        "optimize",
        "pdf_ready",
        "completed",
    ]
    pool = active_ids or ADVISOR_IDS
    for d in range(days, -1, -1):
        # Weekday heavier usage; run_scale thins Pre-SSO / BYOK activity.
        n = max(1, int(round((3 + (d % 5)) * run_scale)))
        if _d(d).weekday() >= 5:
            n = max(1, n - 2)
        for i in range(n):
            aid = pool[(d + i * 3) % len(pool)]
            phase = phases[min(len(phases) - 1, (d + i) % 7)]
            status = "completed" if phase in {"pdf_ready", "completed"} else "running"
            if (d + i) % 11 == 0:
                status = "failed"
                phase = "scenario_parse"
            rows.append(
                {
                    "id": str(uuid4()),
                    "advisor_id": aid,
                    "status": status,
                    "phase": phase,
                    "created_at": _d(d, 9 + (i % 8)),
                    "has_pdf": phase in {"pdf_ready", "completed"},
                    "has_opti": phase in {"optimize", "pdf_ready", "completed"},
                }
            )
    return pd.DataFrame(rows)


def _staged_trades(runs: pd.DataFrame) -> pd.DataFrame:
    """Institutional-sized notionals calibrated for marketing-viable unit economics.

    Target band (30d mock): fee revenue (8 bps) / LLM cost ≈ 12–25×.
    """
    tickers = [
        ("AAPL", "EQUITY", "BUY"),
        ("0700.HK", "EQUITY", "BUY"),
        ("BTC-USD", "CRYPTO", "BUY"),
        ("SOL-USD", "CRYPTO", "SELL"),
        ("MSFT", "EQUITY", "SELL"),
        ("USD", "CASH", "BUY"),
        ("XRP-USD", "CRYPTO", "BUY"),
        ("NVDA", "EQUITY", "BUY"),
    ]
    # Per-lot notionals in USD (PB ticket sizes, not retail qty×spot).
    lot_usd = {
        "AAPL": 250_000,
        "0700.HK": 180_000,
        "BTC-USD": 120_000,
        "SOL-USD": 75_000,
        "MSFT": 300_000,
        "USD": 500_000,
        "XRP-USD": 60_000,
        "NVDA": 280_000,
    }
    rows: list[dict[str, Any]] = []
    sample = runs.sample(min(80, len(runs)), random_state=7) if len(runs) else runs
    for i, (_, run) in enumerate(sample.iterrows()):
        t, ac, side = tickers[i % len(tickers)]
        notion = lot_usd[t] * (0.85 + (i % 5) * 0.08)
        qty = round(notion / max(lot_usd[t] / 100, 1), 2)
        rule_status = "Rejected" if i % 8 == 0 else ("Flagged" if i % 5 == 0 else "Clear")
        rows.append(
            {
                "id": str(uuid4()),
                "order_id": f"ST-{1000 + i}",
                "scenario_run_id": run["id"],
                "advisor_id": run["advisor_id"],
                "client_alias": f"Client_{(i % 9) + 1}",
                "status": "Pending",
                "ticker": t,
                "asset_class": ac,
                "side": side,
                "quantity": qty,
                "notional_value": round(notion, 2),
                "rule_status": rule_status,
                "rule_category": (
                    "POSITION_LIMIT"
                    if i % 3 == 0
                    else ("TRADING_RESTRICTION" if i % 3 == 1 else "CLIENT_RESTRICTION")
                ),
                "created_at": run["created_at"],
            }
        )
    return pd.DataFrame(rows)


def _ratings(runs: pd.DataFrame) -> pd.DataFrame:
    completed = runs[runs["has_opti"]].copy()
    if completed.empty:
        return pd.DataFrame(
            columns=["advisor_id", "scenario_run_id", "rating", "created_at", "comment"]
        )
    sample = completed.sample(min(40, len(completed)), random_state=3)
    comments = [
        "Opti book looked sensible for the client.",
        "PDF layout is clear for the IC memo.",
        "Would like tighter crypto commentary.",
        "Scenario parse needed a small tweak.",
        "Fast path from import to proposal.",
        None,
        None,
    ]
    rows = []
    for i, (_, r) in enumerate(sample.iterrows()):
        score = [5, 5, 4, 5, 4, 3, 5, 5, 4][i % 9]
        rows.append(
            {
                "advisor_id": r["advisor_id"],
                "scenario_run_id": r["id"],
                "rating": score,
                "created_at": r["created_at"],
                "comment": comments[i % len(comments)],
            }
        )
    return pd.DataFrame(rows)


def _param_diffs(runs: pd.DataFrame) -> pd.DataFrame:
    """Advisor edits vs agent params — HITL / deviation signal."""
    rows: list[dict[str, Any]] = []
    for i, (_, r) in enumerate(runs.iterrows()):
        if i % 4 != 0:
            continue
        rows.append(
            {
                "scenario_run_id": r["id"],
                "advisor_id": r["advisor_id"],
                "field_path": [
                    "scenario.tone",
                    "scenario.lookback_window",
                    "risk_aversion",
                    "weight_overrides.AAPL",
                ][i % 4],
                "is_exact_match": False,
                "created_at": r["created_at"],
            }
        )
    return pd.DataFrame(rows)


def _portfolios() -> pd.DataFrame:
    rows = []
    for i, aid in enumerate(ADVISOR_IDS):
        rows.append(
            {
                "id": str(uuid4()),
                "advisor_id": aid,
                "import_hitl": i % 6 == 0,
                "updated_at": _d(i % 20),
            }
        )
    return pd.DataFrame(rows)


def _trading_rules() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"rule_name": "max_single_name_weight", "rule_category": "POSITION_LIMIT", "is_active": True},
            {"rule_name": "max_crypto_sleeve", "rule_category": "POSITION_LIMIT", "is_active": True},
            {"rule_name": "restricted_list", "rule_category": "TRADING_RESTRICTION", "is_active": True},
            {"rule_name": "client_mandate_cash", "rule_category": "CLIENT_RESTRICTION", "is_active": True},
        ]
    )


def _token_usage(
    days: int,
    *,
    commercial_mult: float = 1.0,
    open_mult: float = 1.0,
) -> pd.DataFrame:
    """Synthetic commercial vs open LLM burn sized for ~12–25× fee-revenue / cost.

    ~$700–950/mo blended for the 30d mock (pairs with ~$14k fee revenue @ 8 bps).
    """
    rows = []
    for d in range(days, -1, -1):
        commercial = int((2_100_000 + (d % 7) * 180_000 + (d % 3) * 90_000) * commercial_mult)
        open_llm = int((480_000 + (d % 5) * 70_000) * open_mult)
        if 10 <= d <= 18:
            commercial = int(commercial * 0.35)
            open_llm = int(open_llm * 1.8)
        rows.append(
            {
                "date": _d(d).date().isoformat(),
                "commercial_tokens": commercial,
                "open_tokens": open_llm,
                "commercial_cost_usd": round(commercial / 1_000_000 * 12.0, 2),
                "open_cost_usd": round(open_llm / 1_000_000 * 0.4, 2),
            }
        )
    return pd.DataFrame(rows)
