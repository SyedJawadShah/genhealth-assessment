from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.main import create_app
from app.settings import Settings

LIMIT = 64


class Item(BaseModel):
    name: str
    count: int


@pytest.fixture
def handled() -> list[str]:
    return []


@pytest.fixture
def limited(handled: list[str]) -> Iterator[TestClient]:
    app: FastAPI = create_app(Settings(_env_file=None, app_env="test", max_request_bytes=LIMIT))

    @app.post("/items")
    def create_item(item: Item) -> dict[str, str]:
        handled.append(item.name)
        return {"name": item.name}

    @app.post("/raw")
    async def raw(request: Request) -> dict[str, int]:
        body = await request.body()
        handled.append("raw")
        return {"bytes": len(body)}

    @app.get("/count")
    def count(n: int) -> dict[str, int]:
        return {"n": n}

    with TestClient(app) as client:
        yield client


def _chunks(*parts: bytes) -> Iterator[bytes]:
    yield from parts


def test_body_within_limit_is_accepted(limited: TestClient, handled: list[str]) -> None:
    response = limited.post("/items", json={"name": "a", "count": 1})

    assert response.status_code == 200
    assert handled == ["a"]


def test_declared_oversized_body_is_refused_unread(limited: TestClient, handled: list[str]) -> None:
    response = limited.post(
        "/items", content=b"x" * (LIMIT + 1), headers={"content-type": "application/json"}
    )

    assert response.status_code == 413
    assert response.json() == {
        "error": "request_too_large",
        "request_id": response.headers["X-Request-ID"],
    }
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert handled == []


def test_streamed_oversized_json_body_is_refused(limited: TestClient, handled: list[str]) -> None:
    body = _chunks(b'{"name": "', b"x" * LIMIT, b'", "count": 1}')

    response = limited.post("/items", content=body, headers={"content-type": "application/json"})

    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"
    assert handled == []


def test_streamed_oversized_raw_body_is_refused(limited: TestClient, handled: list[str]) -> None:
    response = limited.post("/raw", content=_chunks(b"x" * 40, b"x" * 40))

    assert response.status_code == 413
    assert handled == []


def test_query_validation_error_does_not_echo_input(limited: TestClient) -> None:
    response = limited.get("/count", params={"n": "sentinel-query-value"})

    assert response.status_code == 422
    assert response.json() == {
        "error": "invalid_request",
        "request_id": response.headers["X-Request-ID"],
        "problems": [{"field": "query.n", "problem": "int_parsing"}],
    }
    assert "sentinel" not in response.text


def test_body_validation_error_does_not_echo_input(limited: TestClient, handled: list[str]) -> None:
    response = limited.post("/items", json={"name": "sentinel-a", "count": "sentinel-b"})

    assert response.status_code == 422
    assert response.json()["problems"] == [{"field": "body.count", "problem": "int_parsing"}]
    assert "sentinel" not in response.text
    assert handled == []
