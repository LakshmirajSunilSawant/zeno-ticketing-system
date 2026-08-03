# WHY middleware rather than per-route decorators: cross-cutting concerns (tracing, logging,
# security headers) must apply to EVERY route including ones I add later. Middleware makes that
# impossible to forget.
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.logging_config import get_logger

log = get_logger("http")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request ID, times the request, and emits one structured access log line.

    WHY a request ID: it's the thread that ties a user's bug report, the access log, and the error
    log together. We honour an inbound X-Request-ID so a gateway/front-end trace ID survives, and
    echo it back on the response so the client can quote it.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        request.state.request_id = request_id

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # WHY log then re-raise: the exception handler owns the HTTP response; this middleware
            # only owns the timing/telemetry, so it must not swallow the error.
            log.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                latency_ms=round((time.perf_counter() - start) * 1000, 2),
            )
            structlog.contextvars.clear_contextvars()
            raise

        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        # WHY expose latency as a header too: makes it trivial to eyeball in curl/Postman demos.
        response.headers["X-Response-Time-ms"] = str(latency_ms)

        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            latency_ms=latency_ms,
            client=request.client.host if request.client else None,
        )
        structlog.contextvars.clear_contextvars()
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """WHY: cheap, standard hardening. Each header closes one specific browser-side attack class."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        # Stops browsers MIME-sniffing a JSON response into executable HTML/JS.
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Legacy clickjacking defence; the CSP frame-ancestors below is the modern equivalent.
        response.headers["X-Frame-Options"] = "DENY"
        # Don't leak our URLs (which contain IDs) to third-party sites via the Referer header.
        response.headers["Referrer-Policy"] = "no-referrer"
        # This is a JSON API - nothing should ever be framed or load scripts from it.
        # WHY the /docs exception: Swagger UI loads its JS/CSS from a CDN and a strict CSP breaks it.
        if not request.url.path.startswith(("/docs", "/redoc")):
            response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        # WHY conditional HSTS: only meaningful over TLS, and forcing it on localhost http breaks dev.
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
