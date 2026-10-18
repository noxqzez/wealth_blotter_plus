"""Data access for the exec monitor — Supabase service-role with mock fallback."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import streamlit as st

from frontend_streamlit.exec_monitor.mock_data import (
    DEFAULT_LOGIN_SCENARIO,
    mock_bundle,
)

# Rough commercial blended rate for mock unit economics when live usage is absent.
_USD_PER_M_COMMERCIAL = 12.0
_USD_PER_M_OPEN = 0.40


def _secret(name: str, default: str = "") -> str:
    env = os.environ.get(name, "")
    if env:
        return env
    try:
        return str(st.secrets.get(name, default))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return default


def supabase_configured() -> bool:
    return bool(_secret("SUPABASE_URL") and _secret("SUPABASE_SERVICE_ROLE_KEY"))


def _client() -> Any | None:
    if not supabase_configured():
        return None
    try:
        from supabase import create_client

        return create_client(_secret("SUPABASE_URL"), _secret("SUPABASE_SERVICE_ROLE_KEY"))
    except Exception:  # noqa: BLE001
        return None


def period_start(period: str) -> datetime:
    now = datetime.now(timezone.utc)
    if period == "Today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "Last 7 Days":
        return now - timedelta(days=7)
    if period == "Year-to-Date":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return now - timedelta(days=30)


@st.cache_data(ttl=30, show_spinner=False)
def load_monitor_bundle(
    period: str = "Last 30 Days",
    login_scenario: str = DEFAULT_LOGIN_SCENARIO,
) -> dict[str, Any]:
    """Load live metrics when possible; otherwise realistic mock data."""
    start = period_start(period)
    client = _client()
    if client is None:
        bundle = mock_bundle(
            days=_days_for_period(period),
            login_scenario=login_scenario,
        )
        return _filter_bundle(bundle, start)

    try:
        bundle = _fetch_live(client, start)
        bundle["source"] = "supabase"
        bundle["login_scenario"] = None
        bundle["system_status"] = {
            "supabase": "Connected",
            "rules_engine": "Active",
            "agent_engine": "Healthy",
        }
        return bundle
    except Exception as exc:  # noqa: BLE001
        bundle = mock_bundle(
            days=_days_for_period(period),
            login_scenario=login_scenario,
        )
        bundle["system_status"] = {
            "supabase": f"Fallback ({type(exc).__name__})",
            "rules_engine": "Active",
            "agent_engine": "Healthy",
        }
        return _filter_bundle(bundle, start)


def _days_for_period(period: str) -> int:
    if period == "Today":
        return 1
    if period == "Last 7 Days":
        return 7
    if period == "Year-to-Date":
        return max(30, datetime.now(timezone.utc).timetuple().tm_yday)
    return 30


def _filter_bundle(bundle: dict[str, Any], start: datetime) -> dict[str, Any]:
    out = dict(bundle)

    def _cut(df: pd.DataFrame, col: str = "created_at") -> pd.DataFrame:
        if df is None or df.empty or col not in df.columns:
            return df
        ts = pd.to_datetime(df[col], utc=True, errors="coerce")
        return df.loc[ts >= pd.Timestamp(start)].copy()

    out["scenario_runs"] = _cut(bundle["scenario_runs"])
    out["staged_trades"] = _cut(bundle["staged_trades"])
    out["interaction_ratings"] = _cut(bundle["interaction_ratings"])
    out["scenario_param_diffs"] = _cut(bundle["scenario_param_diffs"])
    if "token_daily" in bundle and not bundle["token_daily"].empty:
        td = bundle["token_daily"].copy()
        td["date"] = pd.to_datetime(td["date"], utc=True, errors="coerce")
        out["token_daily"] = td.loc[td["date"] >= pd.Timestamp(start)].copy()
    return out


def _fetch_live(client: Any, start: datetime) -> dict[str, Any]:
    iso = start.isoformat()
    runs_raw = (
        client.table("scenario_runs")
        .select(
            "id,advisor_id,status,phase,created_at,performance_series,talking_points,opti_folio"
        )
        .gte("created_at", iso)
        .order("created_at", desc=True)
        .limit(2000)
        .execute()
        .data
        or []
    )
    trades_raw = (
        client.table("staged_trades")
        .select(
            "id,order_id,scenario_run_id,advisor_id,client_alias,status,ticker,"
            "asset_class,side,quantity,notional_value,metadata,created_at"
        )
        .gte("created_at", iso)
        .order("created_at", desc=True)
        .limit(5000)
        .execute()
        .data
        or []
    )
    ratings_raw = (
        client.table("interaction_ratings")
        .select("advisor_id,scenario_run_id,rating,created_at")
        .gte("created_at", iso)
        .order("created_at", desc=True)
        .limit(2000)
        .execute()
        .data
        or []
    )
    diffs_raw = (
        client.table("scenario_param_diffs")
        .select("scenario_run_id,advisor_id,field_path,is_exact_match,created_at")
        .gte("created_at", iso)
        .limit(5000)
        .execute()
        .data
        or []
    )
    portfolios_raw = (
        client.table("portfolios")
        .select("id,advisor_id,import_hitl,updated_at")
        .limit(2000)
        .execute()
        .data
        or []
    )
    rules_raw = (
        client.table("trading_rules")
        .select("rule_name,rule_category,is_active")
        .eq("is_active", True)
        .limit(500)
        .execute()
        .data
        or []
    )
    configs_raw = client.table("configs").select("key,config,version").execute().data or []

    runs = pd.DataFrame(runs_raw)
    if runs.empty:
        runs = pd.DataFrame(
            columns=[
                "id",
                "advisor_id",
                "status",
                "phase",
                "created_at",
                "has_pdf",
                "has_opti",
            ]
        )
    else:
        runs["created_at"] = pd.to_datetime(runs["created_at"], utc=True, errors="coerce")
        runs["has_pdf"] = runs.apply(
            lambda r: bool(r.get("performance_series")) or bool(r.get("talking_points")),
            axis=1,
        )
        runs["has_opti"] = runs["opti_folio"].apply(
            lambda x: isinstance(x, list) and len(x) > 0
        )
        runs["phase"] = runs.get("phase", "completed").fillna("completed")

    trades = pd.DataFrame(trades_raw)
    if trades.empty:
        trades = pd.DataFrame(
            columns=[
                "id",
                "order_id",
                "advisor_id",
                "ticker",
                "side",
                "quantity",
                "notional_value",
                "rule_status",
                "rule_category",
                "created_at",
                "status",
                "client_alias",
                "asset_class",
            ]
        )
    else:
        trades["created_at"] = pd.to_datetime(trades["created_at"], utc=True, errors="coerce")
        trades["rule_status"] = trades.apply(_infer_rule_status, axis=1)
        trades["rule_category"] = trades.apply(_infer_rule_category, axis=1)

    ratings = pd.DataFrame(ratings_raw)
    if not ratings.empty:
        ratings["created_at"] = pd.to_datetime(ratings["created_at"], utc=True, errors="coerce")
        ratings["comment"] = None
    else:
        ratings = pd.DataFrame(
            columns=["advisor_id", "scenario_run_id", "rating", "created_at", "comment"]
        )

    diffs = pd.DataFrame(diffs_raw)
    if not diffs.empty:
        diffs["created_at"] = pd.to_datetime(diffs["created_at"], utc=True, errors="coerce")
    else:
        diffs = pd.DataFrame(
            columns=[
                "scenario_run_id",
                "advisor_id",
                "field_path",
                "is_exact_match",
                "created_at",
            ]
        )

    portfolios = pd.DataFrame(portfolios_raw) if portfolios_raw else pd.DataFrame(
        columns=["id", "advisor_id", "import_hitl", "updated_at"]
    )
    rules = pd.DataFrame(rules_raw) if rules_raw else pd.DataFrame(
        columns=["rule_name", "rule_category", "is_active"]
    )

    advisor_ids = sorted(
        {
            str(x)
            for x in list(runs.get("advisor_id", []))
            + list(trades.get("advisor_id", []))
            + list(portfolios.get("advisor_id", []))
            if x
        }
    )
    advisors = {aid: f"Advisor {str(aid)[:8]}" for aid in advisor_ids} or dict(ADVISOR_LABELS)

    configs: dict[str, Any] = {}
    for row in configs_raw:
        configs[str(row.get("key"))] = row.get("config") or {}

    # Token burn is not instrumented yet — synthesize from run volume.
    token_daily = _estimate_tokens_from_runs(runs)

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "advisors": advisors,
        "scenario_runs": runs,
        "staged_trades": trades,
        "interaction_ratings": ratings,
        "scenario_param_diffs": diffs,
        "portfolios": portfolios,
        "trading_rules": rules,
        "token_daily": token_daily,
        "configs": configs,
    }


def _infer_rule_status(row: pd.Series) -> str:
    meta = row.get("metadata") or {}
    if isinstance(meta, dict):
        for key in ("rule_status", "compliance_status", "status_detail"):
            val = meta.get(key)
            if val:
                return str(val)
        if meta.get("rejected") or meta.get("rule_rejected"):
            return "Rejected"
        if meta.get("flagged") or meta.get("hitl"):
            return "Flagged"
    return "Clear"


def _infer_rule_category(row: pd.Series) -> str:
    meta = row.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("rule_category"):
        return str(meta["rule_category"])
    ac = str(row.get("asset_class") or "").upper()
    if ac == "CRYPTO":
        return "POSITION_LIMIT"
    if str(row.get("ticker") or "").upper() in {"USD", "HKD", "CASH"}:
        return "CLIENT_RESTRICTION"
    return "TRADING_RESTRICTION"


def _estimate_tokens_from_runs(runs: pd.DataFrame) -> pd.DataFrame:
    if runs.empty:
        return pd.DataFrame(
            columns=[
                "date",
                "commercial_tokens",
                "open_tokens",
                "commercial_cost_usd",
                "open_cost_usd",
            ]
        )
    df = runs.copy()
    df["date"] = pd.to_datetime(df["created_at"], utc=True).dt.date
    daily = df.groupby("date").size().rename("runs").reset_index()
    rows = []
    for _, r in daily.iterrows():
        n = int(r["runs"])
        commercial = n * 95_000
        open_tok = n * 40_000
        rows.append(
            {
                "date": r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]),
                "commercial_tokens": commercial,
                "open_tokens": open_tok,
                "commercial_cost_usd": round(commercial / 1_000_000 * _USD_PER_M_COMMERCIAL, 2),
                "open_cost_usd": round(open_tok / 1_000_000 * _USD_PER_M_OPEN, 2),
            }
        )
    return pd.DataFrame(rows)


def compute_kpis(
    bundle: dict[str, Any],
    *,
    advisor_filter: list[str] | None = None,
) -> dict[str, Any]:
    runs = _filter_advisors(bundle["scenario_runs"], advisor_filter)
    trades = _filter_advisors(bundle["staged_trades"], advisor_filter)
    ratings = _filter_advisors(bundle["interaction_ratings"], advisor_filter)
    diffs = _filter_advisors(bundle["scenario_param_diffs"], advisor_filter)
    portfolios = _filter_advisors(bundle.get("portfolios", pd.DataFrame()), advisor_filter)
    advisors_map: dict[str, str] = bundle.get("advisors") or {}

    total_onboarded = max(len(advisors_map), 1)
    active = set()
    if not runs.empty and "advisor_id" in runs:
        active |= set(runs["advisor_id"].dropna().astype(str))
    if not trades.empty and "advisor_id" in trades:
        active |= set(trades["advisor_id"].dropna().astype(str))

    staged_value = 0.0
    if not trades.empty and "notional_value" in trades:
        staged_value = float(pd.to_numeric(trades["notional_value"], errors="coerce").fillna(0).sum())
    # Estimated fee capture (8 bps) — used for revenue / cost unit economics.
    trade_revenue = staged_value * 0.0008

    # Automation = share of runs without advisor param edits and without import HITL
    n_runs = len(runs)
    edited_runs = set()
    if not diffs.empty:
        edited = diffs[diffs["is_exact_match"] == False]  # noqa: E712
        edited_runs = set(edited["scenario_run_id"].dropna().astype(str))
    import_hitl_advisors = set()
    if not portfolios.empty and "import_hitl" in portfolios:
        import_hitl_advisors = set(
            portfolios.loc[portfolios["import_hitl"] == True, "advisor_id"]  # noqa: E712
            .dropna()
            .astype(str)
        )
    hitl_runs = {
        str(r["id"])
        for _, r in runs.iterrows()
        if str(r.get("id")) in edited_runs or str(r.get("advisor_id")) in import_hitl_advisors
    }
    automation = 100.0 if n_runs == 0 else max(0.0, 100.0 * (1.0 - len(hitl_runs) / n_runs))

    avg_rating = None
    if not ratings.empty and "rating" in ratings:
        avg_rating = float(pd.to_numeric(ratings["rating"], errors="coerce").mean())

    rejected = 0
    if not trades.empty and "rule_status" in trades:
        rejected = int((trades["rule_status"].astype(str).str.lower() == "rejected").sum())

    return {
        "active_advisors": len(active),
        "total_onboarded": total_onboarded,
        "activation_rate_pct": 100.0 * len(active) / total_onboarded,
        "staged_order_value": staged_value,
        "trade_revenue": trade_revenue,
        "automation_rate_pct": automation,
        "avg_rating": avg_rating,
        "n_runs": n_runs,
        "n_trades": len(trades),
        "n_rejected_trades": rejected,
        "n_param_edits": len(diffs[diffs["is_exact_match"] == False]) if not diffs.empty else 0,  # noqa: E712
        "n_ratings": len(ratings),
    }


def load_scenario_wf02_config() -> dict[str, Any]:
    """Read configs.scenario_wf02 (empty dict on mock / failure)."""
    client = _client()
    if client is None:
        return {}
    try:
        res = (
            client.table("configs")
            .select("config")
            .eq("key", "scenario_wf02")
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return {}
        cfg = rows[0].get("config") or {}
        return dict(cfg) if isinstance(cfg, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def persist_past_analogue_flag(*, enabled: bool) -> tuple[bool, str]:
    """Write feature_flags.past_analogue. Returns (ok, message). Easy rollback: set false."""
    client = _client()
    if client is None:
        return False, "Supabase not configured — flag not persisted (UI-only)."
    try:
        cfg = load_scenario_wf02_config()
        if not cfg:
            return False, "configs.scenario_wf02 not found."
        if cfg.get("past_analogue_allowed") is False:
            return False, "Past Analogue disabled post-pilot (past_analogue_allowed=false)."
        phase = str(cfg.get("phase") or "").lower()
        if phase in {"post_pilot", "production", "ga"}:
            return False, f"Past Analogue locked off for phase={phase}."
        flags = dict(cfg.get("feature_flags") or {})
        flags["past_analogue"] = bool(enabled)
        # Apply follows master switch (tilts + Tavily only when enabled).
        flags["past_analogue_apply"] = bool(enabled)
        cfg["feature_flags"] = flags
        client.table("configs").update({"config": cfg}).eq(
            "key", "scenario_wf02"
        ).execute()
        return True, (
            "Past Analogue enabled (tilts apply to Opti Folio)."
            if enabled
            else "Past Analogue disabled (no Tavily / no tilts)."
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"Failed to persist flag: {exc}"


def _filter_advisors(df: pd.DataFrame, advisor_filter: list[str] | None) -> pd.DataFrame:
    """``None`` = all advisors; ``[]`` = match none; otherwise exact id set."""
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df
    if advisor_filter is None:
        return df
    if not advisor_filter or "advisor_id" not in df.columns:
        return df.iloc[0:0].copy()
    return df.loc[df["advisor_id"].astype(str).isin(set(advisor_filter))].copy()
