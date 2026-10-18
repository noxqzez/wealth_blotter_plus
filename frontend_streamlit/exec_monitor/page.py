"""Wealth Blotter Plus — Usage and Control Monitor (manager exec view)."""

from __future__ import annotations

import html
from typing import Any

import pandas as pd
import streamlit as st

from frontend_streamlit.exec_monitor.auth import require_manager
from frontend_streamlit.exec_monitor.charts import (
    show_area_wide,
    show_bar,
    show_donut_as_bars,
    show_grouped_bars,
    show_line_wide,
)
from frontend_streamlit.exec_monitor.data import compute_kpis, load_monitor_bundle
from frontend_streamlit.exec_monitor.mock_data import (
    DEFAULT_LOGIN_SCENARIO,
    LOGIN_SCENARIOS,
)
from frontend_streamlit.exec_monitor.theme import inject_css, inject_html, section_title


def render_exec_monitor() -> None:
    inject_css()

    manager = require_manager()
    if manager is None:
        st.stop()

    st.sidebar.markdown("### Control tower")
    st.sidebar.caption("Filters and cost controls apply to KPIs and telemetry charts.")

    with st.sidebar.expander("Cost & revenue controls", expanded=True):
        token_cap = st.slider(
            "Global monthly token cap",
            min_value=100_000,
            max_value=10_000_000,
            value=2_000_000,
            step=100_000,
            help="Usage progress and chart labels use this cap.",
        )
        coin_temp = st.slider(
            "Crypto enthusiasm",
            min_value=1,
            max_value=5,
            value=2,
            help="Scales crypto-related token load on telemetry charts (1=cold … 3=neutral … 5=hot).",
        )
        peg_revenue = st.toggle("Peg token usage to revenue", value=False)
        route_open = st.toggle("Route all requests to open LLM", value=False)

    with st.sidebar.expander("Past Analogue (pilot)", expanded=False):
        from frontend_streamlit.exec_monitor.data import (
            load_scenario_wf02_config,
            persist_past_analogue_flag,
        )

        wf02 = load_scenario_wf02_config()
        flags = dict(wf02.get("feature_flags") or {})
        phase = str(wf02.get("phase") or "phase1").lower()
        allowed = wf02.get("past_analogue_allowed", True) is not False
        locked = (not allowed) or phase in {"post_pilot", "production", "ga"}
        current = bool(flags.get("past_analogue", False))
        if locked:
            st.caption("Locked off after pilot — set past_analogue_allowed / phase in configs.")
            st.toggle("Enable Past Analogue", value=False, disabled=True)
        else:
            pa_on = st.toggle(
                "Enable Past Analogue",
                value=current,
                help=(
                    "When on: Tavily + Ollama find similar past periods and apply "
                    "tilts to Opti Folio (trading/compliance rules still enforced). "
                    "Turn off to skip Tavily and leave weights unchanged."
                ),
            )
            if pa_on != current:
                ok, msg = persist_past_analogue_flag(enabled=pa_on)
                if ok:
                    st.success(msg)
                else:
                    st.warning(msg)

    period = st.sidebar.selectbox(
        "Time period",
        ["Today", "Last 7 Days", "Last 30 Days", "Year-to-Date"],
        index=2,
    )

    scenario_names = list(LOGIN_SCENARIOS.keys())
    default_idx = (
        scenario_names.index(DEFAULT_LOGIN_SCENARIO)
        if DEFAULT_LOGIN_SCENARIO in scenario_names
        else 0
    )
    login_scenario = st.sidebar.selectbox(
        "Login / activation scenario",
        scenario_names,
        index=default_idx,
        help="Mock narrative for Activation Rate: Pre-SSO vs Post-SSO vs BYOK.",
    )
    st.sidebar.caption(LOGIN_SCENARIOS[login_scenario]["blurb"])

    bundle = load_monitor_bundle(period, login_scenario=login_scenario)
    advisors_map: dict[str, str] = bundle.get("advisors") or {}
    st.session_state["wbp_advisor_labels"] = advisors_map

    with st.sidebar.expander("Advisor filter", expanded=False):
        name_query = st.text_input(
            "Advisor name (exact)",
            value="",
            placeholder="input advisor name",
            key="wbp_advisor_name",
            help="Exact display name match — one advisor at a time.",
        ).strip()
        rev = {v: k for k, v in advisors_map.items()}
        if not name_query:
            advisor_filter: list[str] | None = None  # all
            st.caption("Showing all advisors.")
        elif name_query in rev:
            advisor_filter = [rev[name_query]]
            st.caption(f"Filtered to: {name_query}")
        else:
            advisor_filter = []  # no match → empty results
            known = ", ".join(sorted(rev.keys())[:8])
            more = "…" if len(rev) > 8 else ""
            st.warning(f"No exact match for “{name_query}”. Known: {known}{more}")

    ui_controls = {
        "token_cap": token_cap,
        "coin_temperature": coin_temp,
        "peg_revenue": peg_revenue,
        "route_open": route_open,
    }

    left, right = st.columns([3, 2])
    with left:
        inject_html(
            '<div class="wbp-title">Wealth Blotter Plus Usage and Control Monitor</div>'
            '<div class="wbp-subtitle">Executive Control Tower · Adoption, '
            "Risk Guardrails &amp; Unit Economics</div>"
        )
    status = bundle.get("system_status") or {}
    with right:
        inject_html(_status_badges(status, source=str(bundle.get("source", "unknown"))))

    kpis = compute_kpis(bundle, advisor_filter=advisor_filter)
    economics = _unit_economics(bundle, advisor_filter=advisor_filter, ui_controls=ui_controls)
    _hero_kpis(kpis, economics)

    _section_adoption(bundle, advisor_filter=advisor_filter, kpis=kpis)
    _section_economics(bundle, advisor_filter=advisor_filter, ui_controls=ui_controls, economics=economics)
    _section_risk(bundle, advisor_filter=advisor_filter, kpis=kpis)


def _status_badges(status: dict[str, str], *, source: str) -> str:
    sb = status.get("supabase", "Unknown")
    ok = "ok" if "Connected" in str(sb) or source == "mock" else "warn"
    if source == "mock":
        sb_label = "Supabase: Mock data"
        ok = "warn"
    else:
        sb_label = f"Supabase: {sb}"
    return (
        f'<span class="wbp-badge wbp-badge-{ok}">{html.escape(sb_label)}</span>'
        f'<span class="wbp-badge wbp-badge-ok">Rules Engine: '
        f'{html.escape(str(status.get("rules_engine", "Active")))}</span>'
        f'<span class="wbp-badge wbp-badge-ok">Agent Engine: '
        f'{html.escape(str(status.get("agent_engine", "Healthy")))}</span>'
    )


def _hero_kpis(kpis: dict[str, Any], economics: dict[str, Any]) -> None:
    margin = economics.get("margin_pct")
    margin_txt = f"{margin:.1f}%" if margin is not None else "—"
    cards = [
        (
            "Trade revenue",
            _money(economics["trade_revenue"]),
            f"{kpis['n_trades']} trades · 8 bps of notional",
        ),
        (
            "Token costs",
            _money(economics["total_cost"]),
            "Estimated model spend",
        ),
        (
            "Margin %",
            margin_txt,
            "(Revenue − cost) / revenue",
        ),
    ]
    cols = st.columns(3)
    for col, (label, value, hint) in zip(cols, cards):
        with col:
            _kpi_card(label, value, hint)


def _kpi_card(label: str, value: str, hint: str) -> None:
    inject_html(
        f'<div class="wbp-kpi"><div class="wbp-kpi-label">{html.escape(label)}</div>'
        f'<div class="wbp-kpi-value">{html.escape(value)}</div>'
        f'<div class="wbp-kpi-hint">{html.escape(hint)}</div></div>'
    )


def _section_adoption(
    bundle: dict[str, Any],
    *,
    advisor_filter: list[str] | None,
    kpis: dict[str, Any],
) -> None:
    section_title("Business adoption &amp; value delivery")
    runs = _af(bundle["scenario_runs"], advisor_filter)
    st.session_state["wbp_advisor_labels"] = bundle.get("advisors") or {}

    c1, c2 = st.columns(2)
    with c1:
        weekly = _weekly_activity(runs)
        if weekly.empty:
            st.info("No scenario activity in this period.")
        else:
            show_line_wide(
                weekly,
                x="week",
                title="Weekly active advisors vs completed proposals",
            )
    with c2:
        funnel = _funnel(runs)
        if funnel.empty:
            st.info("No funnel data.")
        else:
            show_bar(
                funnel,
                x="stage",
                y="count",
                title="Workflow funnel (import → PDF)",
            )

    adoption_cards = [
        (
            "Activation rate",
            f"{kpis['activation_rate_pct']:.0f}%",
            f"{kpis['active_advisors']} / {kpis['total_onboarded']} active · "
            f"{bundle.get('login_scenario') or 'live'}",
        ),
        (
            "Automation rate",
            f"{kpis['automation_rate_pct']:.1f}%",
            "No advisor interaction",
        ),
        (
            "Advisor satisfaction",
            (
                f"{kpis['avg_rating']:.1f} / 5.0"
                if kpis.get("avg_rating") is not None
                else "—"
            ),
            f"{kpis['n_ratings']} interaction ratings",
        ),
    ]
    cols = st.columns(3)
    for col, (label, value, hint) in zip(cols, adoption_cards):
        with col:
            _kpi_card(label, value, hint)


def _section_economics(
    bundle: dict[str, Any],
    *,
    advisor_filter: list[str] | None,
    ui_controls: dict[str, Any],
    economics: dict[str, Any],
) -> None:
    section_title("Unit economics &amp; cost control")

    tokens = bundle.get("token_daily")
    if tokens is None or tokens.empty:
        st.info("No token series available.")
        _economics_footnote(ui_controls, economics)
        return

    td = _apply_telemetry_controls(tokens, ui_controls)
    c1, c2 = st.columns([2, 1])
    with c1:
        wide_tok = td[["date", "commercial_tokens", "open_tokens"]].rename(
            columns={
                "commercial_tokens": "Commercial LLM",
                "open_tokens": "Open / local LLM",
            }
        )
        show_area_wide(
            wide_tok,
            x="date",
            title=f"Daily tokens (cap {ui_controls['token_cap'] / 1_000_000:.0f}M monthly)",
            y_in_millions=True,
        )
        month_tokens = int(td["commercial_tokens"].sum() + td["open_tokens"].sum())
        st.progress(
            min(1.0, month_tokens / max(ui_controls["token_cap"], 1)),
            text=f"Period usage {month_tokens:,.0f} / cap {ui_controls['token_cap']:,.0f}",
        )
    with c2:
        commercial_cost = float(td["commercial_cost_usd"].sum())
        open_cost = float(td["open_cost_usd"].sum())
        if ui_controls.get("peg_revenue") and economics["trade_revenue"] > 0:
            total = commercial_cost + open_cost
            cap = economics["trade_revenue"] * 0.06
            if total > cap > 0:
                scale = cap / total
                commercial_cost *= scale
                open_cost *= scale
        spend = pd.DataFrame(
            [
                {"route": "Commercial", "usd": commercial_cost},
                {"route": "Open / local", "usd": open_cost},
            ]
        )
        show_donut_as_bars(
            spend,
            category="route",
            value="usd",
            title="Estimated model spend ($)",
        )

    _economics_footnote(ui_controls, economics)


def _economics_footnote(ui_controls: dict[str, Any], economics: dict[str, Any]) -> None:
    peg = "on" if ui_controls.get("peg_revenue") else "off"
    route = "open" if ui_controls.get("route_open") else "mixed"
    inject_html(
        '<div class="wbp-footnote">'
        "Note — Revenue uses 8 bps of trade notional; token cost is estimated "
        f"(~$12/M commercial, ~$0.40/M open). "
        f"Notional {_money(economics['notional'])} · "
        f"coin temp {ui_controls['coin_temperature']}/5 · "
        f"peg-to-revenue {peg} · route {route}."
        "</div>"
    )


def _coin_temp_load(level: int) -> float:
    """Token-load multiplier vs neutral (3). Matches dial semantics: 1 cold … 5 hot."""
    dial = max(1, min(5, int(level or 3)))
    return {1: 0.70, 2: 0.85, 3: 1.00, 4: 1.15, 5: 1.30}[dial]


def _apply_telemetry_controls(
    tokens: pd.DataFrame,
    ui_controls: dict[str, Any],
) -> pd.DataFrame:
    """Apply sidebar dials to the daily token/cost series shown in charts."""
    td = tokens.copy()
    td["date"] = pd.to_datetime(td["date"])
    load = _coin_temp_load(int(ui_controls.get("coin_temperature") or 3))
    for col in (
        "commercial_tokens",
        "open_tokens",
        "commercial_cost_usd",
        "open_cost_usd",
    ):
        if col in td.columns:
            td[col] = pd.to_numeric(td[col], errors="coerce").fillna(0) * load

    if ui_controls.get("route_open"):
        commercial = td["commercial_tokens"].copy()
        commercial_cost = td["commercial_cost_usd"].copy()
        td["commercial_tokens"] = (commercial * 0.3).astype(int)
        td["open_tokens"] = (td["open_tokens"] + commercial * 0.7).astype(int)
        td["commercial_cost_usd"] = commercial_cost * 0.3
        td["open_cost_usd"] = td["open_cost_usd"] + commercial_cost * 0.7 * (0.4 / 12.0)
    else:
        td["commercial_tokens"] = td["commercial_tokens"].astype(int)
        td["open_tokens"] = td["open_tokens"].astype(int)
    return td


def _unit_economics(
    bundle: dict[str, Any],
    *,
    advisor_filter: list[str] | None,
    ui_controls: dict[str, Any],
) -> dict[str, Any]:
    """Fee revenue, model cost, and gross margin for hero KPI cards."""
    _FEE_BPS = 8.0
    tokens = bundle.get("token_daily")
    trades = _af(bundle["staged_trades"], advisor_filter)

    notional = 0.0
    if not trades.empty and "notional_value" in trades:
        notional = float(
            pd.to_numeric(trades["notional_value"], errors="coerce").fillna(0).sum()
        )
    trade_revenue = notional * (_FEE_BPS / 10_000.0)

    total_cost = 0.0
    if tokens is not None and not tokens.empty:
        td = _apply_telemetry_controls(tokens, ui_controls)
        total_cost = float(td["commercial_cost_usd"].sum() + td["open_cost_usd"].sum())
        if ui_controls.get("peg_revenue") and trade_revenue > 0:
            total_cost = min(total_cost, trade_revenue * 0.06)

    margin_pct: float | None
    if trade_revenue > 0:
        margin_pct = 100.0 * (trade_revenue - total_cost) / trade_revenue
    else:
        margin_pct = None

    return {
        "notional": notional,
        "trade_revenue": trade_revenue,
        "total_cost": total_cost,
        "margin_pct": margin_pct,
    }


def _section_risk(
    bundle: dict[str, Any],
    *,
    advisor_filter: list[str] | None,
    kpis: dict[str, Any],
) -> None:
    section_title("Risk, compliance &amp; HITL audit")
    trades = _af(bundle["staged_trades"], advisor_filter)
    portfolios = _af(bundle.get("portfolios", pd.DataFrame()), advisor_filter)

    if trades.empty or "rule_category" not in trades.columns:
        st.info("No trade compliance triggers in period.")
    else:
        trig = (
            trades.groupby(["rule_category", "rule_status"], dropna=False)
            .size()
            .reset_index(name="count")
        )
        wide = trig.pivot_table(
            index="rule_category",
            columns="rule_status",
            values="count",
            fill_value=0,
        )
        show_grouped_bars(wide, title="Compliance pre-check triggers by category")

    hitl_pct = 0.0
    if not portfolios.empty and "import_hitl" in portfolios:
        hitl_pct = 100.0 * float(portfolios["import_hitl"].mean())

    risk_cards = [
        (
            "Rule-rejected trades",
            str(kpis.get("n_rejected_trades", 0)),
            "Blocked by trading rules",
        ),
        (
            "Param edits (HITL)",
            str(kpis.get("n_param_edits", 0)),
            "Advisor scenario overrides",
        ),
        (
            "Import HITL rate",
            f"{hitl_pct:.1f}%",
            "Portfolios needing review",
        ),
    ]
    cols = st.columns(3)
    for col, (label, value, hint) in zip(cols, risk_cards):
        with col:
            _kpi_card(label, value, hint)


# ----- transforms -----


def _af(df: pd.DataFrame, advisor_filter: list[str] | None) -> pd.DataFrame:
    """``None`` = all advisors; ``[]`` = match none; otherwise exact id set."""
    if df is None or df.empty:
        return pd.DataFrame()
    if advisor_filter is None:
        return df
    if not advisor_filter or "advisor_id" not in df.columns:
        return df.iloc[0:0].copy()
    return df.loc[df["advisor_id"].astype(str).isin(set(advisor_filter))].copy()


def _weekly_activity(runs: pd.DataFrame) -> pd.DataFrame:
    """Wide frame for line chart (week + series columns)."""
    if runs.empty:
        return pd.DataFrame(
            columns=["week", "Weekly active advisors", "PDF / completed proposals"]
        )
    df = runs.copy()
    df["week"] = pd.to_datetime(df["created_at"], utc=True).dt.to_period("W").dt.start_time
    active = (
        df.groupby("week")["advisor_id"].nunique().rename("Weekly active advisors").reset_index()
    )
    completed = df[df.get("has_pdf", False) | (df.get("status") == "completed")]
    props = (
        completed.groupby("week").size().rename("PDF / completed proposals").reset_index()
        if not completed.empty
        else pd.DataFrame(columns=["week", "PDF / completed proposals"])
    )
    merged = active.merge(props, on="week", how="outer").fillna(0)
    return merged.sort_values("week")


def _funnel(runs: pd.DataFrame) -> pd.DataFrame:
    if runs.empty:
        return pd.DataFrame(columns=["stage", "count"])
    n = len(runs)
    parsed = int((runs["status"].astype(str) != "failed").sum()) if "status" in runs else n
    opti = int(runs["has_opti"].sum()) if "has_opti" in runs else max(1, int(parsed * 0.85))
    pdf = int(runs["has_pdf"].sum()) if "has_pdf" in runs else max(1, int(opti * 0.8))
    return pd.DataFrame(
        [
            {"stage": "Import", "count": n},
            {"stage": "Base folio", "count": n},
            {"stage": "Scenario parse", "count": parsed},
            {"stage": "BL optimization", "count": opti},
            {"stage": "PDF / complete", "count": pdf},
        ]
    )


def _money(value: float) -> str:
    v = float(value or 0)
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"
