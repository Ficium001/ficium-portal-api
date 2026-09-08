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
    database_url:        str = Field(..., description="Portal Postgres DSN")
    db_pool_min:         int = 5
    db_pool_max:         int = 10
    db_command_timeout:  float = 30.0

    # ── Cross-project: Ficium App database (marketplace pool) ──
    # The marketplace reads public.requests / public.client_financial_snapshot
    # which live in the Ficium App Supabase project, NOT the Portal project.
    # Set to the App project's transaction-pooler DSN (port 6543).
    # Optional: if empty, marketplace endpoints return 503 rather than 500.
    app_database_url:    str = Field(default="", description="Ficium App Postgres DSN")

    # Ficium App's Supabase project (for verifying borrower session tokens via
    # GoTrue introspection — see core/app_auth.py). Different signer than
    # ficium-auth, so this is a separate trust path, not an extension of
    # auth_jwks_url above. anon key only — never the service_role key.
    app_supabase_url:      str = Field(default="", description="Ficium App Supabase project URL")
    app_supabase_anon_key: str = Field(default="", description="Ficium App Supabase anon key")

    @field_validator("database_url", "app_database_url", mode="before")
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
    auth_audience:   str = "authenticated"  # matches ficium-auth aud claim
    jwks_cache_ttl:  int = 3600

    # ── Server-to-server ──────────────────────────────────────
    # Shared secret for calls from the ficium client-app Vercel backend.
    # Set APP_SERVICE_SECRET to the same value in both services.
    app_service_secret: str = Field(default="", description="X-Service-Secret for s2s calls")

    # ── CORS ──────────────────────────────────────────────────
    allowed_origins:      str = "https://ficium-portal.vercel.app,https://portal.ficium.net,https://ficium.vercel.app"
    allowed_origin_regex: str = r"^https://(ficium-portal[a-z0-9.\-]*\.vercel\.app|ficium[a-z0-9.\-]*\.vercel\.app|[a-z0-9.\-]*\.ficium\.net)$"

    # ── Deployment ────────────────────────────────────────────
    deployment_model: str = "saas"
    log_level:        str = "INFO"


settings = Settings()  # type: ignore[call-arg]
