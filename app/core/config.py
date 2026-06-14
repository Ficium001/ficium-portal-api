# =============================================================================
# ficium-portal-api — Configuration
# =============================================================================

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    env:     str = "production"
    version: str = "0.1.0"

    # ── Database ──────────────────────────────────────────────
    # SaaS: Supabase transaction pooler (port 6543, not 5432)
    # Use: postgresql://postgres.[ref]:[password]@aws-0-*.pooler.supabase.com:6543/postgres
    database_url:        str = Field(..., description="Postgres DSN")
    db_pool_min:         int = 2
    db_pool_max:         int = 10
    db_command_timeout:  float = 30.0

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_db_url(cls, v: str) -> str:
        """
        Ensure the DSN uses the plain postgresql:// scheme (psycopg2).
        Strip asyncpg/psycopg2 driver prefixes if present.
        """
        if not isinstance(v, str):
            return v
        v = v.replace("postgresql+asyncpg://", "postgresql://")
        v = v.replace("postgresql+psycopg2://", "postgresql://")
        return v

    # ── Auth ──────────────────────────────────────────────────
    auth_jwks_url:   str = "https://ficium-auth-production.up.railway.app/.well-known/jwks.json"
    auth_issuer:     str = "ficium-auth"
    auth_audience:   str = "authenticated"
    jwks_cache_ttl:  int = 3600

    # ── CORS ──────────────────────────────────────────────────
    allowed_origins:      str = ""
    allowed_origin_regex: str = r"^https://ficium-portal[a-z0-9.\-]*\.vercel\.app$"

    # ── Deployment ────────────────────────────────────────────
    deployment_model: str = "saas"
    log_level:        str = "INFO"


settings = Settings()  # type: ignore[call-arg]
