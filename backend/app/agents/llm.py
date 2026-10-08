"""LLM factory — Ollama by default; BYOK cloud keys via request context."""

from __future__ import annotations

from crewai import LLM

from backend.app.config import Settings, get_settings
from backend.app.core.byok import get_byok, require_byok_for_llm


def build_llm(
    *,
    temperature: float | None = None,
    settings: Settings | None = None,
) -> LLM:
    """
    Build a CrewAI LLM.

    Production / pilot: request-scoped BYOK
    (``X-LLM-API-Key`` + ``X-LLM-Provider`` = openai | anthropic).
    Local development: Ollama (or ``LLM_PROVIDER`` cloud fallback) when no BYOK.
    """
    cfg = settings or get_settings()
    temp = cfg.llm_temperature if temperature is None else temperature
    byok = get_byok()

    if byok and byok.api_key:
        provider = (byok.provider or "openai").strip().lower()
        if provider == "anthropic":
            return LLM(
                model="anthropic/claude-sonnet-4-20250514",
                api_key=byok.api_key,
                temperature=temp,
            )
        # Default BYOK path: OpenAI-compatible (incl. openai)
        return LLM(
            model="openai/gpt-4o-mini",
            api_key=byok.api_key,
            temperature=temp,
        )

    # Raises in production / REQUIRE_BYOK when header missing.
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
