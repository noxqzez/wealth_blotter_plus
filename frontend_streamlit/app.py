"""Streamlit ops dashboard — compliance investigation chat (Phase 1)."""

from __future__ import annotations

import html
import json
import os
from typing import Any

import httpx
import streamlit as st

st.set_page_config(
    page_title="Wealth Blotter Plus — Ops",
    layout="wide",
    initial_sidebar_state="expanded",
)

API_BASE = os.environ.get("WBP_API_BASE", "http://localhost:8000/api/v1")


def _load_jwt() -> str:
    env_jwt = os.environ.get("WBP_API_JWT", "")
    if env_jwt:
        return env_jwt
    try:
        return str(st.secrets.get("WBP_API_JWT", ""))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — secrets.toml may be absent locally
        return ""


JWT = _load_jwt()


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if JWT:
        headers["Authorization"] = f"Bearer {JWT}"
    return headers


def _safe_text(text: str) -> str:
    """Escape HTML so Streamlit markdown cannot inject raw markup."""
    return html.escape(text or "", quote=True)


def _api(method: str, path: str, **kwargs: Any) -> Any:
    url = f"{API_BASE}{path}"
    with httpx.Client(timeout=60.0) as client:
        response = client.request(method, url, headers=_headers(), **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"{response.status_code}: {response.text[:500]}")
        if response.status_code == 204 or not response.content:
            return None
        return response.json()


# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------
page = st.sidebar.radio(
    "Section",
    ["Compliance chat", "Ops overview"],
    index=0,
)
st.sidebar.caption(f"API: `{API_BASE}`")
st.sidebar.caption("Auth: Bearer JWT via `WBP_API_JWT` or secrets; or API `DEV_AUTH_BYPASS`.")

if page == "Ops overview":
    st.title("Wealth Blotter Plus — Operations")
    st.caption("Scaffold: HITL review, agent debug, and blotter ops.")
    left, right = st.columns(2)
    with left:
        st.subheader("HITL queue")
        st.info("Low-confidence agent runs (`confidence_score < 0.85`) will list here.")
    with right:
        st.subheader("Agent debug")
        st.info("Split-screen traces, self-heal retries, and accuracy diffs.")
    st.stop()

# ---------------------------------------------------------------------------
# Compliance chat
# ---------------------------------------------------------------------------
st.title("Compliance investigation")
st.caption(
    "Phase 1: query `audit_events` and cite `trading_rules` / `configs`. "
    "Try: *Does log ID aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeee0001 violate max single-name weight?* "
    "or *Why was yesterday's portfolio_update flagged?*"
)

if "compliance_session_id" not in st.session_state:
    st.session_state.compliance_session_id = None
if "compliance_detail" not in st.session_state:
    st.session_state.compliance_detail = None


def refresh_detail() -> None:
    sid = st.session_state.compliance_session_id
    if not sid:
        st.session_state.compliance_detail = None
        return
    st.session_state.compliance_detail = _api("GET", f"/compliance/chat/sessions/{sid}")


col_list, col_chat = st.columns([1, 2])

with col_list:
    st.subheader("Sessions")
    if st.button("New session", use_container_width=True):
        try:
            created = _api(
                "POST",
                "/compliance/chat/sessions",
                json={"title": "Compliance investigation"},
            )
            st.session_state.compliance_session_id = created["id"]
            refresh_detail()
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(_safe_text(str(exc)))

    try:
        sessions = _api("GET", "/compliance/chat/sessions") or []
    except Exception as exc:  # noqa: BLE001
        sessions = []
        st.warning(_safe_text(f"Could not list sessions: {exc}"))

    for sess in sessions:
        label = sess.get("title") or sess["id"][:8]
        selected = sess["id"] == st.session_state.compliance_session_id
        if st.button(
            f"{'● ' if selected else ''}{_safe_text(str(label))}",
            key=f"sess_{sess['id']}",
            use_container_width=True,
        ):
            st.session_state.compliance_session_id = sess["id"]
            refresh_detail()
            st.rerun()

with col_chat:
    sid = st.session_state.compliance_session_id
    if not sid:
        st.info("Create or select a session to begin.")
    else:
        if st.session_state.compliance_detail is None:
            try:
                refresh_detail()
            except Exception as exc:  # noqa: BLE001
                st.error(_safe_text(str(exc)))

        detail = st.session_state.compliance_detail or {}
        messages = detail.get("messages") or []
        traces = {t["message_id"]: t for t in (detail.get("traces") or [])}

        for msg in messages:
            role = msg.get("role", "assistant")
            with st.chat_message(role if role in {"user", "assistant"} else "assistant"):
                st.markdown(_safe_text(msg.get("content", "")))
                if role == "assistant" and msg["id"] in traces:
                    trace = traces[msg["id"]]
                    with st.expander("Sources & Audit Logs Used"):
                        citations = trace.get("citations") or []
                        if not citations:
                            st.write("No citations.")
                        for cite in citations:
                            parts = []
                            if cite.get("audit_event_id"):
                                parts.append(f"event `{cite['audit_event_id']}`")
                            if cite.get("rule_id"):
                                parts.append(
                                    f"rule `{cite.get('rule_name') or cite['rule_id']}`"
                                )
                            if cite.get("config_key"):
                                parts.append(
                                    f"config `{cite['config_key']}` "
                                    f"v{cite.get('config_version')}"
                                )
                            note = cite.get("note") or ""
                            st.markdown(
                                "- "
                                + _safe_text(", ".join(parts))
                                + (f" — {_safe_text(note)}" if note else "")
                            )
                        st.caption("Normalized query params")
                        st.code(
                            json.dumps(
                                trace.get("normalized_query_params") or {},
                                indent=2,
                            ),
                            language="json",
                        )

        prompt = st.chat_input("Ask about an audit log or portfolio action…")
        if prompt:
            try:
                _api(
                    "POST",
                    f"/compliance/chat/sessions/{sid}/messages",
                    json={"content": prompt},
                )
                refresh_detail()
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(_safe_text(str(exc)))

        st.divider()
        if st.button("Export session audit trace"):
            try:
                export = _api("GET", f"/compliance/chat/sessions/{sid}/export")
                st.download_button(
                    "Download JSON",
                    data=json.dumps(export, indent=2, default=str),
                    file_name=f"compliance_session_{sid}.json",
                    mime="application/json",
                )
                if export.get("markdown"):
                    st.download_button(
                        "Download Markdown",
                        data=export["markdown"],
                        file_name=f"compliance_session_{sid}.md",
                        mime="text/markdown",
                    )
            except Exception as exc:  # noqa: BLE001
                st.error(_safe_text(str(exc)))
