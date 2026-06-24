# =============================================================================
# ficium-portal-api — Application entrypoint
# =============================================================================

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.admin import router as admin_router
from .api.benefits import router as benefits_router
from .api.documents import router as documents_router
from .api.approvals import router as approvals_router
from .api.auth_provision import router as auth_provision_router
from .api.catalog import router as catalog_router
from .api.groups import router as groups_router
from .api.institutions import router as institutions_router
from .api.marketplace import router as marketplace_router
from .api.members import router as members_router
from .api.public import router as public_router
from .core.config import settings
from .core.db import close_pool, init_pool

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

_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_origin_regex=settings.allowed_origin_regex or None,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "ok", "service": "ficium-portal-api", "version": settings.version}


app.include_router(admin_router)
app.include_router(institutions_router)
app.include_router(members_router)
app.include_router(approvals_router)
app.include_router(auth_provision_router)
app.include_router(groups_router)
app.include_router(marketplace_router)
app.include_router(catalog_router)
app.include_router(benefits_router)
app.include_router(documents_router)
app.include_router(public_router)   # server-to-server, no JWT
