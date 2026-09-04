# =============================================================================
# ficium-portal-api — Application entrypoint
# =============================================================================

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
from .api.borrower_portfolio import router as borrower_portfolio_router
from .api.catalog import router as catalog_router
from .api.doc_templates.router import router as doc_templates_router
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
from .api.request_chat import router as request_chat_router
from .api.v1.marketplace import router as v1_marketplace_router
from .api.webhooks import router as webhooks_router
from .core.config import settings
from .core.db import close_pool, init_pool
from .core.observability import (
    RequestObservabilityMiddleware,
    configure_logging,
    metrics_endpoint,
)
from .core.ratelimit import limiter
from .core.response_headers import DefaultResponseHeadersMiddleware

configure_logging(env=settings.env, level=settings.log_level)
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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Without this, an unhandled exception (a raw DB error, a bug, anything that
    isn't an HTTPException) propagates past Starlette's ExceptionMiddleware to
    ServerErrorMiddleware, which sits OUTSIDE CORSMiddleware in the default
    middleware stack. Starlette adds user middleware innermost-last, so the
    plain 500 that ServerErrorMiddleware returns never passes back through
    CORSMiddleware and carries no Access-Control-Allow-Origin header. The
    browser then reports the failure as a CORS error, hiding the real 500 and
    making it look like a frontend/config problem when it's a backend bug.

    Registering a handler for the base Exception runs it inside Starlette's
    ExceptionMiddleware instead, which IS wrapped by CORSMiddleware, so the
    resulting response gets CORS headers like any other. This does not change
    what gets returned to a well-behaved client (HTTPException still goes
    through FastAPI's normal path) — it only stops genuine bugs from
    presenting as a browser-side CORS failure.

    Real incident: GET /institution/doc-templates 500ed with "permission
    denied for table doc_template" (a missing GRANT, fixed separately) and
    the browser showed only a CORS error — the actual cause was invisible
    without server logs.
    """
    log.error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error=str(exc),
        error_type=type(exc).__name__,
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


app.add_middleware(SlowAPIMiddleware)
app.add_middleware(DefaultResponseHeadersMiddleware)

_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
app.add_middleware(RequestObservabilityMiddleware)
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


app.add_route("/metrics", metrics_endpoint, methods=["GET"], include_in_schema=False)
app.include_router(admin_router)
app.include_router(admin_public_router)
app.include_router(institutions_router)
app.include_router(members_router)
app.include_router(approvals_router)
app.include_router(approval_engine_router)  # configurable chains (inst:approvals)
app.include_router(auth_provision_router)
app.include_router(groups_router)
app.include_router(marketplace_router)
app.include_router(borrower_portfolio_router)  # borrower-facing facilities (App-authenticated)
app.include_router(request_chat_router)  # per-lender request chat (proxies App DB)
app.include_router(entitlements_router)  # module packaging (plat:billing)
app.include_router(autobid_router)  # auto-bid rules engine (inst:autobid)
app.include_router(catalog_router)
app.include_router(benefits_router)
app.include_router(documents_router)
app.include_router(esign_router)        # e-signature envelopes + public ceremony
app.include_router(pipeline_templates_router)
app.include_router(doc_templates_router)  # document template designer (inst:doctemplates)
app.include_router(pipeline_router)
app.include_router(notifications_router)
app.include_router(public_router)       # server-to-server, no JWT
app.include_router(api_keys_router)     # institution API key management
app.include_router(webhooks_router)     # webhook CRUD + delivery log
app.include_router(v1_marketplace_router)  # /v1/ versioned public API
