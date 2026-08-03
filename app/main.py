# WHY one composition root: every cross-cutting concern (CORS, security headers, rate limiting,
# error envelope, versioned router mounting) is wired in exactly one readable place.
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.database import init_db
from app.errors import AppError, error_payload
from app.logging_config import configure_logging, get_logger
from app.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.ratelimit import limiter
from app.routers import analytics, auth, bookings, events

configure_logging(settings.ENV)
log = get_logger("app")

API_PREFIX = "/api/v1"  # WHY version in the path: it's visible in logs, curl and browser history,
# and lets v1 and v2 run side by side during a migration. Header-based versioning is "purer" but
# invisible and harder to debug, which is the wrong trade for a public API.


@asynccontextmanager
async def lifespan(app: FastAPI):
    # WHY create tables on startup: keeps first deploy to Render a single step. A real service runs
    # `alembic upgrade head` as a release command so schema changes are versioned and reversible.
    init_db()
    log.info("startup", env=settings.ENV, db="sqlite" if settings.is_sqlite else "postgres")
    yield


app = FastAPI(
    title="Zeno Ticketing API",
    description=(
        "Event ticketing backend. Seat allocation is protected against overselling by a "
        "row-level database lock held for the duration of each booking transaction."
    ),
    version="1.0.0",
    lifespan=lifespan,
    # /docs is served by a custom route below so it can use self-hosted assets; see SWAGGER_LOCAL.
    # The spec itself is still generated from the Pydantic schemas, so the docs cannot drift from
    # the implementation - one of the main reasons for choosing FastAPI.
    docs_url=None,
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# --- middleware -------------------------------------------------------------
# NOTE: Starlette runs middleware in REVERSE order of registration, so RequestContext (added last)
# is outermost and therefore times the full request including everything below it.
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    # WHY wide-open CORS here: this API is meant to be callable from any machine or browser in the
    # world for the demo. In production this would be an explicit list of first-party origins,
    # because "*" plus credentials is exactly how you get CSRF-adjacent bugs.
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,  # must stay False while allow_origins is "*" - the spec forbids both.
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-Response-Time-ms", "Idempotent-Replay"],
)
app.add_middleware(RequestContextMiddleware)


# --- error handling ---------------------------------------------------------
# WHY centralised handlers: one guaranteed response shape, and a hard guarantee that a stack trace
# or SQL fragment never reaches a client.
def _rid(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(exc.code, exc.message, _rid(request)),
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError):
    # Pydantic already produced precise per-field errors; we just wrap them in our envelope so the
    # client parses one shape for every failure. 422 = syntactically valid JSON, semantically wrong.
    payload = error_payload("validation_error", "Request payload failed validation", _rid(request))
    payload["error"]["details"] = [
        {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content=payload)


@app.exception_handler(RateLimitExceeded)
def handle_rate_limit(request: Request, exc: RateLimitExceeded):
    # WHY this one is `def` and not `async def` (the others are async): SlowAPIMiddleware resolves
    # the handler synchronously and silently falls back to slowapi's own plain-text 429 if ours is
    # a coroutine. Declaring it sync is what keeps rate-limit errors in the standard envelope.
    # 429 + Retry-After is the contract a well-behaved client backs off on.
    return JSONResponse(
        status_code=429,
        content=error_payload("rate_limit_exceeded", f"Rate limit exceeded: {exc.detail}", _rid(request)),
        headers={"Retry-After": "60"},
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException):
    # Catches framework-raised 404/405 so even "route not found" uses the same envelope.
    codes = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(codes.get(exc.status_code, "http_error"), str(exc.detail), _rid(request)),
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception):
    # THE important one: anything unanticipated is logged in full server-side and reduced to an
    # opaque message client-side. Leaking a traceback leaks table names, file paths and library
    # versions - free reconnaissance for an attacker. The request_id is how support ties the
    # user's report back to the full trace in our logs.
    log.exception("unhandled_exception", path=request.url.path)
    return JSONResponse(
        status_code=500,
        content=error_payload("internal_error", "An internal error occurred", _rid(request)),
    )


# --- API docs ---------------------------------------------------------------
# WHY serve Swagger UI's own JS/CSS from this server instead of a CDN: FastAPI's default /docs
# pulls ~1.5MB from cdn.jsdelivr.net at page load. If the viewer's network, corporate proxy or
# ad-blocker blocks that CDN, the docs page renders completely blank - and the API looks broken
# when it is perfectly healthy. Self-hosting removes a third-party dependency from the one page
# people judge the API by. The Docker build downloads these files (see Dockerfile).
STATIC_DIR = Path(__file__).parent / "static"
SWAGGER_LOCAL = (STATIC_DIR / "swagger-ui-bundle.js").exists()

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/docs", include_in_schema=False)
@limiter.exempt  # loading a docs page fires several asset requests; don't burn the caller's quota
def swagger_ui(request: Request):
    # Falls back to the CDN when the vendored assets aren't present, so `uvicorn app.main:app`
    # still gives working docs on a bare checkout without running the Docker build first.
    cdn = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5"
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} - Swagger UI",
        swagger_js_url="/static/swagger-ui-bundle.js" if SWAGGER_LOCAL else f"{cdn}/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui.css" if SWAGGER_LOCAL else f"{cdn}/swagger-ui.css",
        # Default is an external favicon on fastapi.tiangolo.com - another needless third-party call.
        swagger_favicon_url="/static/favicon-32x32.png" if SWAGGER_LOCAL else f"{cdn}/favicon-32x32.png",
    )


# --- routes -----------------------------------------------------------------
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(events.router, prefix=API_PREFIX)
app.include_router(bookings.router, prefix=API_PREFIX)
app.include_router(analytics.router, prefix=API_PREFIX)


@app.get("/health", tags=["meta"])
@limiter.exempt  # WHY exempt: the platform's health checker must never be rate limited off.
def health(request: Request):
    """Liveness probe for Render. Deliberately does NOT touch the DB - a liveness check that fails
    on a slow database causes the platform to restart a perfectly healthy process."""
    return {"status": "ok", "version": app.version, "env": settings.ENV}


@app.get("/", tags=["meta"])
@limiter.exempt
def root(request: Request):
    return {"service": "Zeno Ticketing API", "docs": "/docs", "api": API_PREFIX}
