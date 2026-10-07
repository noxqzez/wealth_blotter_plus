"""Request-scoped BYOK LLM credentials — never persist or log the key."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from backend.app.config import Settings, get_settings

HEADER_LLM_API_KEY = "X-LLM-API-Key"
HEADER_LLM_PROVIDER = "X-LLM-Provider"

_SENSITIVE_HEADER_NAMES = frozenset(
    {
        HEADER_LLM_API_KEY.lower(),
        "authorization",
        "x-api-key",
        "api-key",
    }
)


@dataclass(frozen=True, slots=True)
class ByokCredentials:
    api_key: str
    provider: str  # openai | anthropic | ollama


_byok_ctx: ContextVar[ByokCredentials | None] = ContextVar("wbp_byok", default=None)


def set_byok(creds: ByokCredentials | None) -> None:
    _byok_ctx.set(creds)


def get_byok() -> ByokCredentials | None:
    return _byok_ctx.get()


def clear_byok() -> None:
    _byok_ctx.set(None)


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Return a copy safe for logs (keys stripped)."""
    if not headers:
        return {}
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in _SENSITIVE_HEADER_NAMES:
            out[k] = "[REDACTED]"
        else:
            out[k] = v
    return out


def extract_byok_from_request(request: Request) -> ByokCredentials | None:
    raw = (request.headers.get(HEADER_LLM_API_KEY) or "").strip()
    if not raw:
        return None
    provider = (request.headers.get(HEADER_LLM_PROVIDER) or "openai").strip().lower()
    if provider not in {"openai", "anthropic"}:
        provider = "openai"
    return ByokCredentials(api_key=raw, provider=provider)


def require_byok_for_llm(*, settings: Settings | None = None) -> ByokCredentials:
    """
    Production / pilot hosting: LLM routes require BYOK header.
    Local development may fall back to Ollama (no key).
    """
    cfg = settings or get_settings()
    creds = get_byok()
    if creds and creds.api_key:
        return creds
    if cfg.environment.lower() == "production" or getattr(cfg, "require_byok", False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "WBP-AUTH-BYOK-REQUIRED",
                "message": (
                    f"Missing {HEADER_LLM_API_KEY} header. "
                    "Phase 1 pilots must provide their own LLM API key."
                ),
            },
        )
    return ByokCredentials(api_key="", provider="ollama")
