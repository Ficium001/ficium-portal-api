# =============================================================================
# ficium-portal-api — Default response headers middleware
# =============================================================================
#
# Ensures every response carries a Cache-Control directive and a charset on
# JSON content types. Institution/portal data is sensitive by default, so the
# baseline is "no-store" unless a route has explicitly set its own
# Cache-Control (e.g. the public market-intelligence endpoint may opt into
# short-lived public caching).
#
# This closes three related DevTools "Issues" panel warnings that showed up
# across the API surface:
#   - "A 'cache-control' header is missing or empty" (error)
#   - "A 'cache-control' header contains directives which are not
#      recommended: 'must-revalidate'" (warning)
#   - "Response should include 'content-type' header" / missing charset

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class DefaultResponseHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # Cache-Control: default to no-store for anything that didn't set
        # its own directive. Never use must-revalidate on its own (Chrome
        # flags it) — pair it with max-age if a route wants revalidation.
        if not response.headers.get("cache-control"):
            response.headers["Cache-Control"] = "no-store"

        # Ensure JSON responses declare charset explicitly.
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("application/json") and "charset" not in content_type:
            response.headers["Content-Type"] = f"{content_type}; charset=utf-8"

        return response
