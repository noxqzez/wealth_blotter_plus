"""LLM factory — Ollama by default; cloud providers via env flags."""

from __future__ import annotations

from crewai import LLM

from backend.app.config import Settings, get_settings


def build_llm(
    *,
    temperature: float | None = None,
    settings: Settings | None = None,
) -> LLM:
    """
    Build a CrewAI LLM.

    Local (default): Ollama OpenAI-compatible endpoint — no cloud API key required.
    Cloud: set USE_CLOUD_LLM=true and OPENAI_API_KEY / ANTHROPIC_API_KEY.
    """
    cfg = settings or get_settings()
    temp = cfg.llm_temperature if temperature is None else temperature

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

    # CrewAI expects ollama/<model> and talks to Ollama's OpenAI-compatible /v1 API
    model_name = cfg.ollama_model
    if not model_name.startswith("ollama/"):
        model_name = f"ollama/{model_name}"

    return LLM(
        model=model_name,
        base_url=cfg.ollama_base_url.rstrip("/"),
        temperature=temp,
    )
