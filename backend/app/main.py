"""The FastAPI application. Docs at /docs, contract at /openapi.json."""

import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from fastapi.openapi.utils import get_openapi

from app.api import (
    routes_accounts,
    routes_admin,
    routes_auth,
    routes_data,
    routes_public,
    routes_sending,
    routes_webhooks,
)
from app.api.deps import ADMIN_PREFIX, has_admin_secret
from app.config import get_settings
from app.errors import install_error_handlers
import logging

from app.logging import account_id_var, configure_logging, get_logger, log_event, request_id_var, user_id_var

log = get_logger("http")

PUBLIC_MODULES = (routes_public, routes_auth, routes_accounts, routes_sending, routes_data, routes_webhooks)

DESCRIPTION = """
Mailvender's REST API: identity, accounts, sending domains, assets, sending, suppressions and cloud sync.

**Authentication** – browsers use the HttpOnly `mv_session` cookie set by `POST /v1/auth/login`, send the active
account in `X-Account-Id`, and add `X-Requested-With: mailvender` to every unsafe request. Integrations use
`Authorization: Bearer <API key>` on endpoints marked "API key: yes".

**Errors** – every error is `{"error": {"code", "message", "fields"?, "details"?, "request_id"}}`. `code` is stable
and machine-readable, `message` is safe to show, `fields` lists validation problems (`field`, `code`, `message`).
Rate-limited responses are 429 with code `rate_limited` (or `sending_limit_exceeded`) and a `Retry-After` header.

**Lists** – `?limit=` (default 25, max 100) and `?cursor=` (from `next_cursor`).

**Formats** – ids are UUIDs; timestamps are UTC ISO 8601.
"""


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()
    app = FastAPI(
        title="Mailvender API",
        version="1.0.0",
        description=DESCRIPTION,
        docs_url="/docs",
        openapi_url="/openapi.json",
        redoc_url=None,
    )
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Account-Id", "X-Requested-With", "Idempotency-Key", "Authorization"],
    )

    @app.middleware("http")
    async def admin_gate(request: Request, call_next):  # type: ignore[no-untyped-def]
        # /v1/admin/* exists only for the admin app's server (ADMIN_PROXY_SECRET). Everyone else gets the
        # same 404 as for any unknown path – before routing, so no 405/422 hints that the routes exist.
        path = request.url.path
        if (path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/")) and not has_admin_secret(request):
            log_event(log, "admin.gate_rejected", logging.WARNING, method=request.method,
                      client=request.client.host if request.client else None)
            return JSONResponse({"error": {"code": "not_found", "message": "Not Found",
                                           "request_id": request_id_var.get()}}, status_code=404)
        return await call_next(request)

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        if len(request_id) > 100:
            request_id = str(uuid.uuid4())
        request_id_var.set(request_id)
        user_id_var.set(None)
        account_id_var.set(None)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.max_request_bytes:
            response = JSONResponse(
                {"error": {"code": "payload_too_large", "message": "The request body is too large.",
                           "request_id": request_id}},
                status_code=413,
            )
        else:
            started = time.perf_counter()
            response = await call_next(request)
            route = request.scope.get("route")
            path = getattr(route, "path", request.url.path)
            if path.startswith("/health/") and response.status_code == 200:
                response.headers["X-Request-ID"] = request_id
                return response  # health probes every few seconds: not worth a log line
            log_event(
                log,
                "http.request",
                method=request.method,
                path=path,
                status=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                user_id=getattr(request.state, "user_id", None),
                account_id=getattr(request.state, "account_id", None),
            )
        response.headers["X-Request-ID"] = request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

    for module in (*PUBLIC_MODULES, routes_admin):
        app.include_router(module.router)

    def public_openapi() -> dict:  # type: ignore[type-arg]
        # The admin API isn't part of the public contract (/docs, /openapi.json); see admin_openapi().
        if app.openapi_schema is None:
            routes = [route for module in PUBLIC_MODULES for route in module.router.routes]
            app.openapi_schema = get_openapi(title=app.title, version=app.version, description=app.description,
                                             routes=routes)
        return app.openapi_schema

    app.openapi = public_openapi  # type: ignore[method-assign]
    return app


def admin_openapi(application: FastAPI) -> dict:  # type: ignore[type-arg]
    """The admin API's contract (`python -m app.cli openapi --admin`), for mailvender-admin's types."""
    return get_openapi(
        title="Mailvender Admin API", version=application.version,
        description="Used only by mailvender-admin through its server, with X-Mailvender-Admin-Secret.",
        routes=routes_admin.router.routes,
    )


app = create_app()
