"""Liveness endpoint used by the container platform's health checks."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.deps import settings_dep as app_settings
from app.settings import Settings

router = APIRouter()


class Health(BaseModel):
    status: Literal["ok"]
    version: str


@router.get("/healthz")
def healthz(settings: Annotated[Settings, Depends(app_settings)]) -> Health:
    return Health(status="ok", version=settings.git_sha)
