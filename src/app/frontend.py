"""Browser frontend: static HTML, CSS and JavaScript served at /app.

The pages call the same REST API as any other client, using a bearer token. They contain no
inline scripts or styles, so they work under the strict Content-Security-Policy.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"


def mount_frontend(app: FastAPI) -> None:
    app.mount("/app", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")
