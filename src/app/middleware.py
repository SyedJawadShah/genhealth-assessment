"""HTTP middleware: request IDs, request logging, security headers and body size limits."""

import logging
import time
import uuid

from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("app.request")


def error_response(
    error: str, status_code: int, request_id: str | None, **extra: object
) -> JSONResponse:
    """The one error shape clients see: a code and the request ID, never internal details."""
    response = JSONResponse(
        {"error": error, "request_id": request_id, **extra}, status_code=status_code
    )
    if request_id:
        response.headers["X-Request-ID"] = request_id
    return response


class RequestTooLargeError(HTTPException):
    """Raised mid-stream when a request body passes the limit.

    It subclasses HTTPException because FastAPI re-raises those from body parsing instead of
    turning them into a generic 400.
    """

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="request_too_large")


class BodySizeLimitMiddleware:
    """Reject request bodies larger than max_bytes before the application reads them.

    A declared Content-Length over the limit is refused without reading the body; bodies sent
    without one are counted as they arrive and stopped at the limit.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            request_id: str | None = scope.get("state", {}).get("request_id")
            await error_response("request_too_large", 413, request_id)(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestTooLargeError
            return message

        await self.app(scope, limited_receive, send)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
HSTS = "max-age=31536000; includeSubDomains"
# Interactive API docs load assets from a CDN; they are only served outside production.
_CSP_EXEMPT_PATHS = frozenset({"/docs"})


def apply_security_headers(response: Response, path: str, *, hsts: bool) -> None:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if path not in _CSP_EXEMPT_PATHS:
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    response.headers.setdefault("Cache-Control", "no-store")
    if hsts:
        response.headers.setdefault("Strict-Transport-Security", HSTS)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        apply_security_headers(response, request.url.path, hsts=self.hsts)
        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request ID and log one line per request, without query strings or bodies."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        finally:
            route = request.scope.get("route")
            log.info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    # The route template ("/items/{item_id}"), not the raw path, so
                    # identifiers in URLs stay out of the logs.
                    "route": getattr(route, "path", "unmatched"),
                    "status": status,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                },
            )
        response.headers["X-Request-ID"] = request_id
        return response
