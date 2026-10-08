"""Health check endpoint."""

from fastapi import APIRouter

from backend.app.config import get_settings

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, object]:
    """Liveness plus safe config probes (no secret values)."""
    settings = get_settings()
    return {
        "status": "ok",
        "environment": settings.environment,
        "supabase_url_configured": bool(settings.supabase_url.strip()),
        "supabase_anon_configured": bool(settings.supabase_anon_key.strip()),
        "supabase_service_role_configured": bool(
            settings.supabase_service_role_key.strip()
        ),
        "supabase_jwt_secret_configured": bool(settings.supabase_jwt_secret.strip()),
        "require_byok": bool(settings.require_byok),
        "require_pilot_approval": bool(settings.require_pilot_approval),
        "dev_auth_bypass": bool(settings.dev_auth_bypass),
        "sso_enabled": bool(settings.sso_enabled),
    }
