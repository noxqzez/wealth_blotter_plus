"""PII anonymization helpers for compliance / LLM-bound text."""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Account-like digit runs (8+ digits, optional separators)
_ACCOUNT_RE = re.compile(r"\b(?:\d[-\s]?){8,}\b")
# Email addresses
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Phrases that look like named clients in free text
_CLIENT_NAME_RE = re.compile(
    r"\b(?:client|customer|account\s*holder)\s*[:#]?\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b",
    re.IGNORECASE,
)


def _hash_token(value: str, *, prefix: str = "anon") -> str:
    digest = hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def anonymize_text(text: str) -> str:
    """
    Strip/mask account numbers, emails, and obvious client-name phrases.

    Safe for outbound LLM payloads and persisted traces. Opaque client_alias
    values that are already non-PII keys are left unchanged unless they match
    account/email patterns.
    """
    if not text:
        return text
    out = _ACCOUNT_RE.sub("[REDACTED_ACCOUNT]", text)
    out = _EMAIL_RE.sub("[REDACTED_EMAIL]", out)

    def _replace_name(match: re.Match[str]) -> str:
        label = match.group(0).split(match.group(1))[0]
        return f"{label}{_hash_token(match.group(1), prefix='client')}"

    out = _CLIENT_NAME_RE.sub(_replace_name, out)
    return out


def anonymize_entity_refs(refs: dict[str, Any]) -> dict[str, Any]:
    """Copy entity_refs with string values scrubbed; never pass secrets."""
    cleaned: dict[str, Any] = {}
    for key, value in refs.items():
        lower = key.lower()
        if any(s in lower for s in ("secret", "password", "token", "api_key", "credential")):
            continue
        if isinstance(value, str):
            if lower in {"client_name", "full_name", "account_number", "ssn"}:
                cleaned[key] = _hash_token(value, prefix="ref")
            else:
                cleaned[key] = anonymize_text(value)
        elif isinstance(value, dict):
            cleaned[key] = anonymize_entity_refs(value)
        else:
            cleaned[key] = value
    return cleaned


def anonymize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Deep-scrub nested payload dicts before persist or LLM handoff."""
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        lower = key.lower()
        if any(s in lower for s in ("secret", "password", "token", "api_key", "credential")):
            continue
        if isinstance(value, str):
            cleaned[key] = anonymize_text(value)
        elif isinstance(value, dict):
            cleaned[key] = anonymize_payload(value)
        elif isinstance(value, list):
            cleaned[key] = [
                anonymize_payload(v)
                if isinstance(v, dict)
                else anonymize_text(v)
                if isinstance(v, str)
                else v
                for v in value
            ]
        else:
            cleaned[key] = value
    return cleaned
