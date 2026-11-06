"""Liveness endpoint used by the container platform's health checks."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.settings import Settings

router = APIRouter()


class Health(BaseModel):
    status: Literal["ok"]
    version: str


def app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


@router.get("/healthz")
def healthz(settings: Annotated[Settings, Depends(app_settings)]) -> Health:
    return Health(status="ok", version=settings.git_sha)
