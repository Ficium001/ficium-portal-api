# =============================================================================
# ficium-portal-api — Configuration
# Environment-driven (pydantic-settings). The SAME image runs in every
# deployment model; only env values differ.
# =============================================================================

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    env:     str = "production"
    version: str = "0.1.0"

    # ── Database ──────────────────────────────────────────────
    # SaaS:        Supabase Postgres direct connection (NOT PostgREST)
    # client-cloud/MRU: managed Postgres in-region
    # on-premises: Postgres inside the institution's environment
    database_url:        str = Field(..., description="Postgres DSN")
    db_pool_min:         int = 2
    db_pool_max:         int = 10
    db_command_timeout:  float = 30.0

    # ── Auth (verify ficium-auth tokens) ──────────────────────
    # JWKS discovery URL of ficium-auth. Verified, cached, refreshed on
    # unknown-kid (key rotation). No shared secret.
    auth_jwks_url:   str = "https://ficium-auth-production.up.railway.app/.well-known/jwks.json"
    auth_issuer:     str = "https://ficium-auth-production.up.railway.app"
    auth_audience:   str = "authenticated"
    jwks_cache_ttl:  int = 3600

    # ── CORS (Portal origins) ─────────────────────────────────
    allowed_origins:       str = ""
    allowed_origin_regex:  str = r"^https://ficium-portal[a-z0-9.\-]*\.vercel\.app$"

    # ── Deployment model (audit/labelling) ────────────────────
    deployment_model: str = "saas"   # saas | client_cloud | on_prem

    log_level: str = "INFO"


settings = Settings()  # type: ignore[call-arg]
