"""FastAPI dependencies that read shared state set up by `create_app`."""

from collections.abc import Iterator

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from app.extraction import Extractor
from app.ratelimit import RateLimiter
from app.settings import Settings


def settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def db_session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] | None = request.app.state.session_factory
    if factory is None:
        # Production without DATABASE_URL: refuse rather than write to the ephemeral disk.
        raise HTTPException(status_code=503, detail="service_unavailable")
    with factory() as session:
        yield session


def token_key(request: Request) -> bytes:
    key: bytes | None = request.app.state.token_key
    if key is None:
        raise HTTPException(status_code=503, detail="service_unavailable")
    return key


def rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


def extractor_dep(request: Request) -> Extractor:
    extractor: Extractor = request.app.state.extractor
    return extractor


def client_ip(request: Request) -> str:
    # Uvicorn runs with --proxy-headers, so this is the client address the load balancer saw.
    return request.client.host if request.client else "unknown"
