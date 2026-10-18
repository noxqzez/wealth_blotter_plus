"""LLM factory — primary workhorse + optional secondary (recommended)."""

from __future__ import annotations

from typing import Literal

from crewai import LLM

from backend.app.config import Settings, get_settings
from backend.app.core.byok import (
    ByokCredentials,
    get_byok,
    get_secondary_byok,
    require_byok_for_llm,
)

LlmPrefer = Literal["primary", "secondary"]

# Defaults when user only supplies a key (no custom model field yet).
_PROVIDER_MODELS: dict[str, str] = {
    "openai": "openai/gpt-4o-mini",
    "anthropic": "anthropic/claude-sonnet-4-20250514",
    "deepseek": "deepseek/deepseek-chat",
    "grok": "xai/grok-2-latest",
    "gemini": "gemini/gemini-2.0-flash",
}

_PROVIDER_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com/v1",
    "grok": "https://api.x.ai/v1",
}


def _llm_from_credentials(
    creds: ByokCredentials,
    *,
    temperature: float,
    settings: Settings,
) -> LLM:
    provider = (creds.provider or "openai").strip().lower()
    base = (creds.base_url or "").strip().rstrip("/")
    key = (creds.api_key or "").strip() or None

    if provider == "ollama":
        model_name = settings.ollama_model
        if not model_name.startswith("ollama/"):
            model_name = f"ollama/{model_name}"
        return LLM(
            model=model_name,
            base_url=base or settings.ollama_base_url.rstrip("/"),
            temperature=temperature,
        )

    if provider == "anthropic":
        return LLM(
            model=_PROVIDER_MODELS["anthropic"],
            api_key=key,
            temperature=temperature,
        )

    if provider == "gemini":
        return LLM(
            model=_PROVIDER_MODELS["gemini"],
            api_key=key,
            temperature=temperature,
        )

    # OpenAI-compatible path (openai, deepseek, grok, or custom base URL).
    model = _PROVIDER_MODELS.get(provider, _PROVIDER_MODELS["openai"])
    if provider == "openai":
        model = _PROVIDER_MODELS["openai"]
    elif provider == "deepseek":
        model = _PROVIDER_MODELS["deepseek"]
    elif provider == "grok":
        model = _PROVIDER_MODELS["grok"]

    resolved_base = base or _PROVIDER_BASE_URLS.get(provider)
    kwargs: dict[str, object] = {
        "model": model,
        "api_key": key,
        "temperature": temperature,
    }
    if resolved_base:
        kwargs["base_url"] = resolved_base
    return LLM(**kwargs)  # type: ignore[arg-type]


def build_llm(
    *,
    temperature: float | None = None,
    settings: Settings | None = None,
    prefer: LlmPrefer = "primary",
) -> LLM:
    """
    Build a CrewAI LLM.

    * **primary** — workhorse (Ollama base URL and/or any BYOK provider).
    * **secondary** — optional recommended model; falls back to primary if unset.

    Local development with no headers: server ``LLM_PROVIDER`` / Ollama.
    """
    cfg = settings or get_settings()
    temp = cfg.llm_temperature if temperature is None else temperature

    if prefer == "secondary":
        secondary = get_secondary_byok()
        if secondary is not None:
            return _llm_from_credentials(secondary, temperature=temp, settings=cfg)
        # Fall through to primary.

    primary = get_byok()
    if primary and primary.is_configured:
        return _llm_from_credentials(primary, temperature=temp, settings=cfg)

    # Raises in production / REQUIRE_BYOK when primary missing.
    require_byok_for_llm(settings=cfg)

    if cfg.use_cloud_llm and cfg.llm_provider.lower() in {"openai", "anthropic"}:
        if cfg.llm_provider.lower() == "openai":
            return LLM(
                model=f"openai/{cfg.ollama_model}",
                api_key=cfg.openai_api_key or None,
                temperature=temp,
            )
        return LLM(
            model=f"anthropic/{cfg.ollama_model}",
            api_key=cfg.anthropic_api_key or None,
            temperature=temp,
        )

    model_name = cfg.ollama_model
    if not model_name.startswith("ollama/"):
        model_name = f"ollama/{model_name}"

    return LLM(
        model=model_name,
        base_url=cfg.ollama_base_url.rstrip("/"),
        temperature=temp,
    )
