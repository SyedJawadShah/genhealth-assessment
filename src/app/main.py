"""Application factory and ASGI entry point (`uvicorn app.main:app`)."""

import html
import logging
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import auth, health, orders
from app.db import make_engine, make_session_factory, run_migrations
from app.extraction import AnthropicExtractor, Extractor, UnconfiguredExtractor
from app.frontend import mount_frontend
from app.logging_setup import configure_logging
from app.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    RequestTooLargeError,
    SecurityHeadersMiddleware,
    apply_security_headers,
    error_response,
)
from app.ratelimit import RateLimiter
from app.settings import Settings, get_settings

log = logging.getLogger(__name__)


def _default_extractor(settings: Settings) -> Extractor:
    if settings.anthropic_api_key is None:
        return UnconfiguredExtractor()
    return AnthropicExtractor(
        api_key=settings.anthropic_api_key.get_secret_value(),
        model=settings.extraction_model,
        effort=settings.extraction_effort,
        timeout_seconds=settings.extraction_timeout_seconds,
        max_retries=settings.extraction_max_retries,
    )


def _token_key(settings: Settings) -> bytes | None:
    if settings.secret_key is not None:
        return settings.secret_key.get_secret_value().encode()
    # Outside production a per-process key is fine: tokens just end at restart.
    return None if settings.is_production else secrets.token_bytes(32)


# Fixed, human-readable text for errors a client can fix; never built from input.
ERROR_MESSAGES = {
    "not_a_pdf": "Only PDF files are accepted.",
    "empty_file": "The uploaded file is empty. Only PDF files are accepted.",
    "file_too_large": "The file is larger than the upload limit.",
}


def _field_path(error: Mapping[str, Any]) -> str:
    parts = [str(part) for part in error.get("loc", ())]
    # An unexpected key is chosen by the client and may itself be personal data.
    if error.get("type") == "extra_forbidden" and parts:
        parts[-1] = "<unknown field>"
    return ".".join(parts)


def _error_code(exc: StarletteHTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, str) and detail.replace("_", "").isalpha() and detail.islower():
        return detail
    return HTTPStatus(exc.status_code).phrase.lower().replace(" ", "_").replace("-", "_")


def _request_id(request: Request) -> str | None:
    request_id: str | None = getattr(request.state, "request_id", None)
    return request_id


def create_app(settings: Settings | None = None, extractor: Extractor | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    database_url = settings.resolved_database_url()
    engine = make_engine(database_url) if database_url else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if engine is None:
            log.error("database_not_configured")
        else:
            try:
                run_migrations(engine)
            except SQLAlchemyError as exc:
                # The driver's first line says why (timeout, auth, DNS) and holds no password.
                reason = str(getattr(exc, "orig", exc)).splitlines()[0][:300]
                log.error(
                    "database_unreachable",
                    extra={"exc_type": type(exc).__name__, "detail": reason},
                )
                raise
            with make_session_factory(engine)() as session:
                for username, password in settings.demo_users.items():
                    auth.upsert_user(session, username.lower(), password.get_secret_value())
                session.commit()
            log.info("startup_complete", extra={"demo_users": len(settings.demo_users)})
        if app.state.token_key is None:
            log.error("secret_key_not_configured")
        yield
        if engine is not None:
            engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=settings.git_sha,
        description=(
            "Upload a DME order PDF and get the patient and ordering provider back, checked. "
            "Use **Authorize** with a username and password to try it."
        ),
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = make_session_factory(engine) if engine else None
    app.state.token_key = _token_key(settings)
    app.state.rate_limiter = RateLimiter()
    app.state.extractor = extractor or _default_extractor(settings)
    # Starlette runs the last-added middleware first, so the size limit sits innermost and
    # its 413 responses still get security headers and a request ID.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(orders.router)
    mount_frontend(app)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Say which field had which kind of problem, but never echo the submitted value:
        # it can be personal data, and FastAPI's default response includes it.
        problems = [
            {"field": _field_path(error), "problem": str(error.get("type", "invalid"))}
            for error in exc.errors()
        ]
        request_id = _request_id(request)
        log.info(
            "invalid_request",
            extra={"request_id": request_id, "fields": [p["field"] for p in problems]},
        )
        return error_response("invalid_request", 422, request_id, problems=problems)

    @app.exception_handler(orders.FieldValidationError)
    async def invalid_fields(request: Request, exc: orders.FieldValidationError) -> JSONResponse:
        return error_response("invalid_request", 422, _request_id(request), problems=exc.problems)

    @app.exception_handler(RequestTooLargeError)
    async def request_too_large(request: Request, _exc: RequestTooLargeError) -> JSONResponse:
        return error_response("request_too_large", 413, _request_id(request))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _error_code(exc)
        extra = {"message": ERROR_MESSAGES[code]} if code in ERROR_MESSAGES else {}
        response = error_response(code, exc.status_code, _request_id(request), **extra)
        for name, value in (exc.headers or {}).items():
            response.headers[name] = value
        return response

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = _request_id(request)
        # Type and request ID only: exception messages and tracebacks can carry patient data.
        log.error(
            "unhandled_error", extra={"request_id": request_id, "exc_type": type(exc).__name__}
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
            f"<body><h1>{name}</h1><p>Version {version}</p>"
            "<p>API documentation and a way to try the upload: <a href='/docs'>/docs</a></p>"
            "</body></html>"
        )

    return app


app = create_app()
