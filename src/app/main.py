"""Application factory and ASGI entry point (`uvicorn app.main:app`)."""

import html
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse

from app import health
from app.logging_setup import configure_logging
from app.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    RequestTooLargeError,
    SecurityHeadersMiddleware,
    apply_security_headers,
    error_response,
)
from app.settings import Settings, get_settings

log = logging.getLogger(__name__)


def _request_id(request: Request) -> str | None:
    request_id: str | None = getattr(request.state, "request_id", None)
    return request_id


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=settings.git_sha,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings
    # Starlette runs the last-added middleware first, so the size limit sits innermost and
    # its 413 responses still get security headers and a request ID.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Say which field had which kind of problem, but never echo the submitted value:
        # it can be personal data, and FastAPI's default response includes it.
        problems = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())),
                "problem": str(error.get("type", "invalid")),
            }
            for error in exc.errors()
        ]
        request_id = _request_id(request)
        log.info(
            "invalid_request",
            extra={"request_id": request_id, "fields": [p["field"] for p in problems]},
        )
        return error_response("invalid_request", 422, request_id, problems=problems)

    @app.exception_handler(RequestTooLargeError)
    async def request_too_large(request: Request, _exc: RequestTooLargeError) -> JSONResponse:
        return error_response("request_too_large", 413, _request_id(request))

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = _request_id(request)
        log.error(
            "unhandled_error",
            exc_info=exc,
            extra={"request_id": request_id, "exc_type": type(exc).__name__},
        )
        # This handler runs outside the middleware stack, so it adds the headers itself.
        response = error_response("internal_error", 500, request_id)
        apply_security_headers(response, request.url.path, hsts=settings.is_production)
        return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        name = html.escape(settings.app_name)
        version = html.escape(settings.git_sha)
        return (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<title>{name}</title></head>"
            f"<body><h1>{name}</h1><p>Version {version}</p></body></html>"
        )

    return app


app = create_app()
