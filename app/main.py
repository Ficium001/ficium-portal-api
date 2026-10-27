# =============================================================================
# ficium-portal-api — Application entrypoint
# =============================================================================

import asyncio
import re
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from . import maintenance
from .api.acceptance import router as acceptance_router
from .api.admin import public_router as admin_public_router
from .api.admin import router as admin_router
from .api.api_keys import router as api_keys_router
from .api.approval_engine import router as approval_engine_router
from .api.approvals import router as approvals_router
from .api.auth_provision import router as auth_provision_router
from .api.autobid import router as autobid_router
from .api.benefits import router as benefits_router
from .api.booking import router as booking_router
from .api.catalog import router as catalog_router
from .api.controls import router as controls_router
from .api.deal_document_uploads import router as deal_document_uploads_router
from .api.deal_documents import router as deal_documents_router
from .api.doc_templates.router import router as doc_templates_router
from .api.documents import router as documents_router
from .api.entitlements import router as entitlements_router
from .api.esign import router as esign_router
from .api.groups import router as groups_router
from .api.institutions import router as institutions_router
from .api.integration import router as integration_router
from .api.internal_admin import router as internal_admin_router
from .api.marketplace import router as marketplace_router
from .api.members import router as members_router
from .api.notifications import router as notifications_router
from .api.onboarding import router as onboarding_router
from .api.pipeline import router as pipeline_router
from .api.pipeline_templates import router as pipeline_templates_router
from .api.portal_badges import router as portal_badges_router
from .api.product_limits import router as product_limits_router
from .api.registration import router as registration_router
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
from .integration import dispatcher as integration_dispatcher

configure_logging(env=settings.env, level=settings.log_level)
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("portal_api_starting", env=settings.env, model=settings.deployment_model)
    init_pool()
    stop = asyncio.Event()
    dispatcher: asyncio.Task[None] | None = None
    if settings.integration_outbound_enabled:
        dispatcher = asyncio.create_task(integration_dispatcher.run_forever(stop))
    maintenance_task: asyncio.Task[None] | None = None
    if settings.maintenance_close_expired_enabled:
        maintenance_task = asyncio.create_task(maintenance.run_forever(stop))
    log.info("portal_api_ready", integration_outbound=settings.integration_outbound_enabled,
             maintenance_close_expired=settings.maintenance_close_expired_enabled,
             integration_inbound=bool(settings.integration_inbound_keys))
    yield
    stop.set()
    if dispatcher is not None:
        await dispatcher
    if maintenance_task is not None:
        await maintenance_task
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


_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
_origin_re = re.compile(settings.allowed_origin_regex) if settings.allowed_origin_regex else None


def _cors_headers(request: Request) -> dict[str, str]:
    """The CORS headers CORSMiddleware would have added for this request."""
    origin = request.headers.get("origin")
    if not origin:
        return {}
    if origin in _origins or (_origin_re is not None and _origin_re.fullmatch(origin)):
        return {"Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Credentials": "true", "Vary": "Origin"}
    return {}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Log an unhandled exception and return a generic 500 the browser can read.

    Starlette routes a handler for the base Exception (or status 500) to
    ServerErrorMiddleware, the OUTERMOST layer, outside CORSMiddleware. So the
    500 never passes back through CORS and the browser reports a CORS error,
    hiding the real failure. (The Aug 2026 version of this handler assumed it
    ran inside CORS; it didn't, and every 500 still showed as CORS: the
    doc-templates missing GRANT, then the workflow editor's broken SQL in
    Oct 2026.) The handler therefore adds the same CORS headers itself, from
    the same allow-list.
    """
    log.error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error=str(exc),
        error_type=type(exc).__name__,
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error."},
                        headers=_cors_headers(request))


app.add_middleware(SlowAPIMiddleware)
app.add_middleware(DefaultResponseHeadersMiddleware)

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
app.include_router(request_chat_router)  # per-lender request chat (proxies App DB)
app.include_router(entitlements_router)  # module packaging (plat:billing)
app.include_router(autobid_router)  # auto-bid rules engine (inst:autobid)
app.include_router(catalog_router)
app.include_router(benefits_router)
app.include_router(documents_router)
app.include_router(esign_router)        # e-signature envelopes + public ceremony
app.include_router(pipeline_templates_router)
app.include_router(deal_documents_router)   # document checklist (db/027)
app.include_router(doc_templates_router)  # document template designer (inst:doctemplates)
app.include_router(booking_router)        # deal booking hand-off (inst:booking)
app.include_router(pipeline_router)
app.include_router(notifications_router)
app.include_router(product_limits_router)
app.include_router(portal_badges_router)
app.include_router(api_keys_router)     # institution API key management
app.include_router(webhooks_router)     # webhook CRUD + delivery log
app.include_router(v1_marketplace_router)  # /v1/ versioned public API
app.include_router(integration_router)     # contract v1 inbound events (signed, B2I)
app.include_router(acceptance_router)      # contract v1 acceptance call (own key, OFF by default)
app.include_router(deal_document_uploads_router)  # contract 1.5 borrower uploads (OFF by default)
app.include_router(controls_router)        # four-eyes switches (Setup > Controls)
app.include_router(internal_admin_router)  # admin site channel: signed + staff MFA token, API only
app.include_router(registration_router)    # self-registration, one identity system
app.include_router(onboarding_router)      # onboarding submit (documents + prefs + stage)
