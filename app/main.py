# =============================================================================
# ficium-portal-api — Application entrypoint
# =============================================================================

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.institutions import router as institutions_router
from .api.members import router as members_router
from .api.approvals import router as approvals_router
from .api.marketplace import router as marketplace_router
from .api.catalog import router as catalog_router
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


app.include_router(institutions_router)
app.include_router(members_router)
app.include_router(approvals_router)
app.include_router(marketplace_router)
app.include_router(catalog_router)
