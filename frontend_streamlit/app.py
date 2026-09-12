"""Streamlit ops & agent debug dashboard (scaffold)."""

from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="Wealth Blotter Plus — Ops",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("Wealth Blotter Plus — Operations")
st.caption("Scaffold: HITL review, agent debug, and blotter ops will plug in here.")

left, right = st.columns(2)

with left:
    st.subheader("HITL queue")
    st.info("Low-confidence agent runs (`confidence_score < 0.85`) will list here.")

with right:
    st.subheader("Agent debug")
    st.info("Split-screen traces, self-heal retries, and accuracy diffs (agent vs advisor).")

st.divider()
st.subheader("Configuration")
st.write(
    "Rules-as-code are stored in Supabase `configs` "
    "(`technical_limits`, `business_guardrails`). "
    "This UI will read them via the FastAPI service later."
)
