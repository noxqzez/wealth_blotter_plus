"""Streamlit ops dashboard — compliance view + executive usage monitor."""

from __future__ import annotations

import html
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import streamlit as st

# Allow `streamlit run frontend_streamlit/app.py` without installing the package.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

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
    [
        "Usage & Control Monitor",
        "Compliance view",
    ],
    index=0,
)

if page == "Usage & Control Monitor":
    from frontend_streamlit.exec_monitor.page import render_exec_monitor

    render_exec_monitor()
    st.stop()

# ---------------------------------------------------------------------------
# Compliance view — audit Q&A text box
# ---------------------------------------------------------------------------
st.title("Compliance view")
st.caption(
    "Enter an audit question about an order, position, or portfolio action. "
    'Examples: “Why did this orderID get blocked?” · '
    '“When was this positionID generated?”'
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


def ensure_session() -> str | None:
    """Reuse the open session or create one so the UI stays a single text box."""
    if st.session_state.compliance_session_id:
        return str(st.session_state.compliance_session_id)
    try:
        created = _api(
            "POST",
            "/compliance/chat/sessions",
            json={"title": "Compliance investigation"},
        )
        st.session_state.compliance_session_id = created["id"]
        refresh_detail()
        return str(created["id"])
    except Exception as exc:  # noqa: BLE001
        st.error(_safe_text(str(exc)))
        return None


sid = st.session_state.compliance_session_id
if sid and st.session_state.compliance_detail is None:
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

prompt = st.chat_input(
    "e.g. Why did orderID … get blocked? When was positionID … generated?"
)
if prompt:
    active = ensure_session()
    if active:
        try:
            _api(
                "POST",
                f"/compliance/chat/sessions/{active}/messages",
                json={"content": prompt},
            )
            refresh_detail()
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(_safe_text(str(exc)))
