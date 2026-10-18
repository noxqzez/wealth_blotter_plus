"""Application settings loaded from environment / `.env`."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration. Secrets must come from env — never hardcode."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    supabase_url: str = Field(default="", alias="SUPABASE_URL")
    supabase_anon_key: str = Field(default="", alias="SUPABASE_ANON_KEY")
    supabase_service_role_key: str = Field(default="", alias="SUPABASE_SERVICE_ROLE_KEY")
    supabase_jwt_secret: str = Field(default="", alias="SUPABASE_JWT_SECRET")

    jwt_secret_key: str = Field(default="change_me", alias="JWT_SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    jwt_audience: str = Field(default="authenticated", alias="JWT_AUDIENCE")

    llm_provider: str = Field(default="ollama", alias="LLM_PROVIDER")
    ollama_base_url: str = Field(default="http://localhost:11434", alias="OLLAMA_BASE_URL")
    ollama_model: str = Field(default="llama3.1", alias="OLLAMA_MODEL")
    llm_temperature: float = Field(default=0.2, alias="LLM_TEMPERATURE")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    use_cloud_llm: bool = Field(default=False, alias="USE_CLOUD_LLM")
    # Past Analogue — search + optional future LLM override (stubs; empty = unused).
    # Tavily: used today when set. Gemini: placeholder until provider is wired.
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    # ollama | openai | anthropic | gemini — gemini not implemented yet (see llm.py).
    past_analogue_llm_provider: str = Field(
        default="",
        alias="PAST_ANALOGUE_LLM_PROVIDER",
    )
    # Pilot thrift: cache Tavily by query; cap live calls per process (0 = unlimited).
    tavily_cache_dir: str = Field(
        default="artifacts/past_analogue/tavily_cache",
        alias="TAVILY_CACHE_DIR",
    )
    tavily_max_calls_per_process: int = Field(
        default=40,
        alias="TAVILY_MAX_CALLS_PER_PROCESS",
        description="Hard cap on live Tavily HTTP calls per API process (pilot default 40).",
    )
    past_analogue_max_search_queries: int = Field(
        default=1,
        alias="PAST_ANALOGUE_MAX_SEARCH_QUERIES",
        description="Queries per scenario (pilot default 1 to save credits; was 2).",
    )

    confidence_threshold: float = Field(default=0.85, alias="CONFIDENCE_THRESHOLD")

    # MVP local demo: skip JWT and use a fixed advisor id
    # NEVER enable in production / Render.
    dev_auth_bypass: bool = Field(default=False, alias="DEV_AUTH_BYPASS")
    dev_advisor_id: str = Field(
        default="00000000-0000-4000-8000-000000000001",
        alias="DEV_ADVISOR_ID",
    )
    # When true (or ENVIRONMENT=production), LLM routes require X-LLM-API-Key.
    require_byok: bool = Field(default=False, alias="REQUIRE_BYOK")
    # When true (or ENVIRONMENT=production), JWT users must be profiles.APPROVED.
    require_pilot_approval: bool = Field(
        default=False, alias="REQUIRE_PILOT_APPROVAL"
    )

    # SSO (Supabase OAuth/SAML) — default OFF. Do not enable in prod until IdP is ready.
    sso_enabled: bool = Field(default=False, alias="SSO_ENABLED")
    sso_providers: str = Field(default="", alias="SSO_PROVIDERS")
    sso_auto_approve_domains: str = Field(
        default="", alias="SSO_AUTO_APPROVE_DOMAINS"
    )
    sso_require_approval: bool = Field(default=True, alias="SSO_REQUIRE_APPROVAL")

    # Host admin (approve + welcome email). Never give this key to pilots.
    admin_api_key: str = Field(default="", alias="ADMIN_API_KEY")
    # Resend.com — transactional welcome email after approval.
    resend_api_key: str = Field(default="", alias="RESEND_API_KEY")
    # Verified sender, e.g. "Wealth Blotter <pilots@yourdomain.com>"
    pilot_welcome_from_email: str = Field(
        default="",
        alias="PILOT_WELCOME_FROM_EMAIL",
    )
    # Full URL to manifest.pilot.xml (preferred).
    pilot_manifest_url: str = Field(
        default="https://wealth-blotter-plus-excel.onrender.com/manifest.pilot.xml",
        alias="PILOT_MANIFEST_URL",
    )
    # Fallback base for Excel static site if PILOT_MANIFEST_URL empty.
    pilot_excel_base_url: str = Field(
        default="https://wealth-blotter-plus-excel.onrender.com",
        alias="PILOT_EXCEL_BASE_URL",
    )

    cors_origins: str = Field(
        default="https://localhost:3000,http://localhost:8501",
        alias="CORS_ORIGINS",
    )

    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")

    rules_cache_ttl_seconds: int = Field(default=60, alias="RULES_CACHE_TTL_SECONDS")

    # RAG POC — Hugging Face sentence-transformers (local). Dim must match pgvector.
    rag_storage_bucket: str = Field(
        default="research-documents",
        alias="RAG_STORAGE_BUCKET",
    )
    rag_embedding_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        alias="RAG_EMBEDDING_MODEL",
    )
    rag_embedding_dims: int = Field(default=384, alias="RAG_EMBEDDING_DIMS")
    rag_max_upload_bytes: int = Field(
        default=20 * 1024 * 1024,
        alias="RAG_MAX_UPLOAD_BYTES",
    )
    rag_chunk_size: int = Field(default=800, alias="RAG_CHUNK_SIZE")
    rag_chunk_overlap: int = Field(default=120, alias="RAG_CHUNK_OVERLAP")
    rag_classify_excerpt_chars: int = Field(
        default=2000,
        alias="RAG_CLASSIFY_EXCERPT_CHARS",
    )
    rag_auto_process_upload: bool = Field(
        default=True,
        alias="RAG_AUTO_PROCESS_UPLOAD",
    )
    # Pilot upload controls (option B): kill switch + rate + daily page quota.
    rag_upload_enabled: bool = Field(default=True, alias="RAG_UPLOAD_ENABLED")
    rag_upload_rate_limit_per_minute: int = Field(
        default=5,
        ge=1,
        alias="RAG_UPLOAD_RATE_LIMIT_PER_MINUTE",
    )
    rag_upload_max_pages_per_day: int = Field(
        default=50,
        ge=1,
        alias="RAG_UPLOAD_MAX_PAGES_PER_DAY",
    )
    rag_upload_max_pages_per_file: int = Field(
        default=50,
        ge=1,
        alias="RAG_UPLOAD_MAX_PAGES_PER_FILE",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def sso_provider_list(self) -> list[str]:
        return [
            p.strip().lower()
            for p in self.sso_providers.split(",")
            if p.strip()
        ]

    @property
    def sso_auto_approve_domain_list(self) -> list[str]:
        return [
            d.strip().lower().lstrip("@")
            for d in self.sso_auto_approve_domains.split(",")
            if d.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
