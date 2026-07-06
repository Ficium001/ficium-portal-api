# =============================================================================
# ficium-portal-api — Application entrypoint
# =============================================================================

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from .api.admin import public_router as admin_public_router
from .api.admin import router as admin_router
from .api.api_keys import router as api_keys_router
from .api.approval_engine import router as approval_engine_router
from .api.approvals import router as approvals_router
from .api.auth_provision import router as auth_provision_router
from .api.autobid import router as autobid_router
from .api.benefits import router as benefits_router
from .api.catalog import router as catalog_router
from .api.documents import router as documents_router
from .api.entitlements import router as entitlements_router
from .api.esign import router as esign_router
from .api.groups import router as groups_router
from .api.institutions import router as institutions_router
from .api.marketplace import router as marketplace_router
from .api.members import router as members_router
from .api.notifications import router as notifications_router
from .api.pipeline import router as pipeline_router
from .api.pipeline_templates import router as pipeline_templates_router
from .api.public import router as public_router
from .api.v1.marketplace import router as v1_marketplace_router
from .api.webhooks import router as webhooks_router
from .core.config import settings
from .core.db import close_pool, init_pool
from .core.ratelimit import limiter
from .core.response_headers import DefaultResponseHeadersMiddleware

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("portal_api_starting", env=settings.env, model=settings.deployment_model)
    init_pool()
    log.info("portal_api_ready")
    yield
    close_pool()
    log.info("portal_api_stopped")


app = FastAPI(
    title="Ficium Portal API",
    version=settings.version,
    lifespan=lifespan,
)

# Rate limiting — per-tenant (institution_id from JWT) with IP fallback.
# Default 600/min per bucket; returns 429 with Retry-After when exceeded.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(DefaultResponseHeadersMiddleware)

_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_origin_regex=settings.allowed_origin_regex or None,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "ok", "service": "ficium-portal-api", "version": settings.version}


app.include_router(admin_router)
app.include_router(admin_public_router)
app.include_router(institutions_router)
app.include_router(members_router)
app.include_router(approvals_router)
app.include_router(approval_engine_router)  # configurable chains (inst:approvals)
app.include_router(auth_provision_router)
app.include_router(groups_router)
app.include_router(marketplace_router)
app.include_router(entitlements_router)  # module packaging (plat:billing)
app.include_router(autobid_router)  # auto-bid rules engine (inst:autobid)
app.include_router(catalog_router)
app.include_router(benefits_router)
app.include_router(documents_router)
app.include_router(esign_router)        # e-signature envelopes + public ceremony
app.include_router(pipeline_templates_router)
app.include_router(pipeline_router)
app.include_router(notifications_router)
app.include_router(public_router)       # server-to-server, no JWT
app.include_router(api_keys_router)     # institution API key management
app.include_router(webhooks_router)     # webhook CRUD + delivery log
app.include_router(v1_marketplace_router)  # /v1/ versioned public API
