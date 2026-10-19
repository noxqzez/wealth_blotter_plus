"""Parse free-text Copilot ops intents."""

from __future__ import annotations

import re
from uuid import UUID

from backend.app.services.copilot_ops_models import OpsQueryMatch, _safe_uuid

_CLIENT_ALIAS_RE = re.compile(
    r"\bclient_alias\s*[:=]?\s*[\"']?([A-Za-z0-9._\-]{1,64})[\"']?",
    re.IGNORECASE,
)
_CLIENT_FOR_RE = re.compile(
    r"\bfor\s+client\s+[\"']?([A-Za-z0-9._\-]{1,64})[\"']?",
    re.IGNORECASE,
)
_CLIENT_BARE_RE = re.compile(
    r"\bclient\s+[\"']?([A-Za-z0-9._\-]{1,64})[\"']?",
    re.IGNORECASE,
)
_PORTFOLIO_ID_RE = re.compile(
    r"\b(?:portfolio(?:_|\s)?id|portfolioid)\s*[:=]?\s*"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{3,})\b",
    re.IGNORECASE,
)
_LOAD_CUES = re.compile(
    r"\b(load(?:\s+it)?|show\s+and\s+load|write\s+to\s+sheet|put\s+on\s+sheet|"
    r"populate|overwrite)\b",
    re.IGNORECASE,
)


def parse_ops_query(text: str) -> OpsQueryMatch | None:
    """Heuristic ops intent. Returns None if not an ops question."""
    lower = (text or "").strip().lower()
    if not lower:
        return None

    want_load = bool(_LOAD_CUES.search(lower))
    client = _extract_client_alias(text)
    portfolio_id, invalid_pid = _extract_portfolio_id(text)

    if re.search(r"\bload\s+it\b", lower) and not any(
        k in lower
        for k in (
            "pdf",
            "portfolio",
            "opti",
            "trade",
            "research",
            "price",
            "enrich",
        )
    ):
        return OpsQueryMatch(kind="load_followup", want_load=True)

    if re.search(r"\b(download|get)\b.*\bpdf\b|\bpdf\b.*\b(download|get)\b", lower):
        return OpsQueryMatch(
            kind="download_pdf",
            client_alias=client,
            portfolio_id=portfolio_id,
            invalid_portfolio_token=invalid_pid,
            want_load=True,
        )

    if re.search(
        r"\b(update|refresh|re-?enrich)\b.*\bprices?\b|"
        r"\bprices?\b.*\b(update|refresh|re-?enrich)\b|"
        r"\bupdate\s+prices\b",
        lower,
    ):
        return OpsQueryMatch(
            kind="update_prices",
            portfolio_id=portfolio_id,
            invalid_portfolio_token=invalid_pid,
            want_load=True,
        )

    if re.search(
        r"\b(optimiz|opti[_\s]?folio|rebalanced)\b.*\bportfolio|"
        r"\boptimized\s+portfolio\b|\bshow\b.*\bopti\b",
        lower,
    ):
        return OpsQueryMatch(
            kind="optimized_portfolio",
            client_alias=client,
            portfolio_id=portfolio_id,
            invalid_portfolio_token=invalid_pid,
            want_load=want_load,
        )

    if re.search(
        r"\b(house|internal)\b.*\bresearch|\bresearch\b.*\b(house|internal|scenario)\b|"
        r"\bwhat\s+did\s+internal\s+research\b",
        lower,
    ):
        return OpsQueryMatch(kind="house_research", want_load=False)

    if re.search(
        r"\b(latest|recent|last)\b.*\btrade|\btrade[s]?\b.*\b(latest|recent|last|for)\b|"
        r"\borders?\b.*\b(client|latest)\b",
        lower,
    ):
        return OpsQueryMatch(
            kind="latest_trades",
            client_alias=client,
            want_load=False,
        )

    if re.search(
        r"\b(latest|current)\b.*\bportfolio\b|\bportfolio\b.*\b(latest|current)\b|"
        r"\bwhat\s+is\s+(the\s+)?latest\s+portfolio\b",
        lower,
    ):
        return OpsQueryMatch(
            kind="latest_portfolio",
            client_alias=client,
            portfolio_id=portfolio_id,
            invalid_portfolio_token=invalid_pid,
            want_load=want_load,
        )

    return None


def _extract_client_alias(text: str) -> str | None:
    for pat in (_CLIENT_ALIAS_RE, _CLIENT_FOR_RE, _CLIENT_BARE_RE):
        m = pat.search(text)
        if m:
            alias = m.group(1).strip()
            if alias.lower() in {
                "the",
                "for",
                "of",
                "pdf",
                "portfolio",
                "trades",
                "alias",
            }:
                continue
            return alias
    return None


def _extract_portfolio_id(text: str) -> tuple[UUID | None, str | None]:
    """Return (uuid, invalid_token). invalid_token set when a non-UUID id was given."""
    m = _PORTFOLIO_ID_RE.search(text)
    if not m:
        return None, None
    raw = m.group(1)
    uid = _safe_uuid(raw)
    if uid is not None:
        return uid, None
    return None, raw
