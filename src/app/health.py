"""Liveness (/healthz) and readiness (/readyz) endpoints."""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.deps import settings_dep as app_settings
from app.extraction import UnconfiguredExtractor
from app.settings import Settings

log = logging.getLogger(__name__)
router = APIRouter()


class Health(BaseModel):
    status: Literal["ok"]
    version: str


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    database: Literal["ok", "unavailable"]
    extraction: Literal["ok", "unconfigured"]


@router.get("/healthz")
def healthz(settings: Annotated[Settings, Depends(app_settings)]) -> Health:
    """Liveness: the process is up. Used by the platform's health checks."""
    return Health(status="ok", version=settings.git_sha)


def _database_ok(factory: sessionmaker[Session] | None) -> bool:
    if factory is None:
        return False
    try:
        with factory() as session:
            session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        log.warning("readiness_database_failed", extra={"exc_type": type(exc).__name__})
        return False
    return True


@router.get("/readyz", responses={503: {"model": Readiness}})
def readyz(request: Request) -> JSONResponse:
    """Readiness: the database answers and extraction is configured."""
    database_ok = _database_ok(request.app.state.session_factory)
    extraction_ok = not isinstance(request.app.state.extractor, UnconfiguredExtractor)
    body = Readiness(
        status="ready" if database_ok and extraction_ok else "not_ready",
        database="ok" if database_ok else "unavailable",
        extraction="ok" if extraction_ok else "unconfigured",
    )
    return JSONResponse(body.model_dump(), status_code=200 if body.status == "ready" else 503)
