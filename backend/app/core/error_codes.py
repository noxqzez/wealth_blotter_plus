"""Stable error codes for support tracing (Excel + API + audit logs).

Format: WBP-<DOMAIN>-<CLASS>
Include ``correlation_id`` (X-Request-ID) when raising to support.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

# --- Portfolio / securities import (WF-01) ------------------------------------
WBP_IMPORT_TIMEOUT = "WBP-IMPORT-TIMEOUT"
WBP_IMPORT_VALIDATE = "WBP-IMPORT-VALIDATE"
WBP_IMPORT_BUILD = "WBP-IMPORT-BUILD"
WBP_IMPORT_SERVER = "WBP-IMPORT-SERVER"
WBP_IMPORT_NETWORK = "WBP-IMPORT-NETWORK"
WBP_IMPORT_CLIENT = "WBP-IMPORT-CLIENT"
WBP_IMPORT_PARSE = "WBP-IMPORT-PARSE"

# --- Market scenarios (WF-02) -------------------------------------------------
WBP_SCENARIO_TIMEOUT = "WBP-SCENARIO-TIMEOUT"
WBP_SCENARIO_PARSE = "WBP-SCENARIO-PARSE"
WBP_SCENARIO_PARAMS = "WBP-SCENARIO-PARAMS"
WBP_SCENARIO_RUN = "WBP-SCENARIO-RUN"
WBP_SCENARIO_PDF = "WBP-SCENARIO-PDF"
WBP_SCENARIO_SERVER = "WBP-SCENARIO-SERVER"
WBP_SCENARIO_NETWORK = "WBP-SCENARIO-NETWORK"
WBP_SCENARIO_CLIENT = "WBP-SCENARIO-CLIENT"
WBP_SCENARIO_BASE_REQUIRED = "WBP-SCENARIO-BASE-REQUIRED"
WBP_SCENARIO_COMPLIANCE = "WBP-SCENARIO-COMPLIANCE"
WBP_SCENARIO_INVALID_PROMPT = "WBP-SCENARIO-INVALID-PROMPT"
WBP_SCENARIO_QUANT_SAFETY = "WBP-SCENARIO-QUANT-SAFETY"

SUPPORT_MESSAGE = (
    "System error, please try again later and raise to support."
)


def new_correlation_id() -> str:
    return str(uuid4())


def error_payload(
    *,
    error_code: str,
    message: str,
    correlation_id: str | None = None,
    support: bool = False,
) -> dict[str, Any]:
    """JSON body fragment for HTTPException.detail / client display."""
    cid = correlation_id or new_correlation_id()
    text = SUPPORT_MESSAGE if support else message
    return {
        "error_code": error_code,
        "message": text,
        "correlation_id": cid,
        "detail": text,  # FastAPI clients that only read detail as string-ish
    }


def format_support_ref(error_code: str, correlation_id: str) -> str:
    short = correlation_id.replace("-", "")[:8]
    return f"{SUPPORT_MESSAGE}\nRef: {error_code} · {short}"
