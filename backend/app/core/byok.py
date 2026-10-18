"""Request-scoped BYOK LLM credentials — never persist or log the key."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from backend.app.config import Settings, get_settings

HEADER_LLM_API_KEY = "X-LLM-API-Key"
HEADER_LLM_PROVIDER = "X-LLM-Provider"
HEADER_LLM_BASE_URL = "X-LLM-Base-URL"
HEADER_LLM_SECONDARY_API_KEY = "X-LLM-Secondary-API-Key"
HEADER_LLM_SECONDARY_PROVIDER = "X-LLM-Secondary-Provider"
HEADER_LLM_SECONDARY_BASE_URL = "X-LLM-Secondary-Base-URL"

PRIMARY_PROVIDERS = frozenset(
    {"ollama", "openai", "anthropic", "deepseek", "grok", "gemini"}
)
SECONDARY_PROVIDERS = frozenset(
    {"openai", "anthropic", "deepseek", "grok", "gemini"}
)

_SENSITIVE_HEADER_NAMES = frozenset(
    {
        HEADER_LLM_API_KEY.lower(),
        HEADER_LLM_SECONDARY_API_KEY.lower(),
        "authorization",
        "x-api-key",
        "api-key",
    }
)


@dataclass(frozen=True, slots=True)
class ByokCredentials:
    api_key: str
    provider: str  # ollama | openai | anthropic | deepseek | grok | gemini
    base_url: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key.strip()) or bool(self.base_url.strip())


@dataclass(frozen=True, slots=True)
class ByokBundle:
    """Primary workhorse + optional secondary (recommended) model."""

    primary: ByokCredentials
    secondary: ByokCredentials | None = None


_byok_ctx: ContextVar[ByokBundle | None] = ContextVar("wbp_byok", default=None)


def set_byok(creds: ByokCredentials | ByokBundle | None) -> None:
    if creds is None:
        _byok_ctx.set(None)
    elif isinstance(creds, ByokBundle):
        _byok_ctx.set(creds)
    else:
        _byok_ctx.set(ByokBundle(primary=creds, secondary=None))


def get_byok_bundle() -> ByokBundle | None:
    return _byok_ctx.get()


def get_byok() -> ByokCredentials | None:
    """Primary credentials (backward-compatible)."""
    bundle = _byok_ctx.get()
    if bundle is None:
        return None
    return bundle.primary


def get_secondary_byok() -> ByokCredentials | None:
    bundle = _byok_ctx.get()
    if bundle is None:
        return None
    sec = bundle.secondary
    if sec is None or not sec.is_configured:
        return None
    return sec


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


def _norm_provider(raw: str, *, allowed: frozenset[str], default: str) -> str:
    p = (raw or "").strip().lower()
    if p not in allowed:
        return default
    return p


def extract_byok_from_request(request: Request) -> ByokBundle | None:
    primary_key = (request.headers.get(HEADER_LLM_API_KEY) or "").strip()
    primary_base = (request.headers.get(HEADER_LLM_BASE_URL) or "").strip()
    primary_provider = _norm_provider(
        request.headers.get(HEADER_LLM_PROVIDER) or "",
        allowed=PRIMARY_PROVIDERS,
        default="openai" if primary_key else "ollama",
    )

    secondary_key = (request.headers.get(HEADER_LLM_SECONDARY_API_KEY) or "").strip()
    secondary_base = (request.headers.get(HEADER_LLM_SECONDARY_BASE_URL) or "").strip()
    secondary_provider = _norm_provider(
        request.headers.get(HEADER_LLM_SECONDARY_PROVIDER) or "",
        allowed=SECONDARY_PROVIDERS,
        default="openai",
    )

    primary = ByokCredentials(
        api_key=primary_key,
        provider=primary_provider,
        base_url=primary_base,
    )
    secondary: ByokCredentials | None = None
    if secondary_key or secondary_base:
        secondary = ByokCredentials(
            api_key=secondary_key,
            provider=secondary_provider,
            base_url=secondary_base,
        )

    if not primary.is_configured and secondary is None:
        return None
    return ByokBundle(primary=primary, secondary=secondary)


def require_byok_for_llm(*, settings: Settings | None = None) -> ByokCredentials:
    """
    Production / pilot hosting: require primary API key **or** primary base URL.
    Local development may fall back to server Ollama (empty credentials).
    """
    cfg = settings or get_settings()
    primary = get_byok()
    if primary and primary.is_configured:
        return primary
    if cfg.environment.lower() == "production" or getattr(cfg, "require_byok", False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "WBP-AUTH-BYOK-REQUIRED",
                "message": (
                    f"Missing primary LLM config. Provide {HEADER_LLM_API_KEY} "
                    f"and/or {HEADER_LLM_BASE_URL} (e.g. local Ollama). "
                    "Secondary LLM is optional."
                ),
            },
        )
    return ByokCredentials(api_key="", provider="ollama", base_url="")
