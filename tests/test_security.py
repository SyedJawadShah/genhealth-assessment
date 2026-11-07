import json
import logging
import sys

import pytest
from fastapi.testclient import TestClient

from app.logging_setup import JsonFormatter, configure_logging
from app.main import create_app
from app.middleware import CONTENT_SECURITY_POLICY, HSTS, SECURITY_HEADERS
from app.settings import Settings


@pytest.mark.parametrize("path", ["/", "/healthz", "/does-not-exist"])
def test_security_headers_on_every_response(client: TestClient, path: str) -> None:
    response = client.get(path)

    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["Cache-Control"] == "no-store"
    assert "Strict-Transport-Security" not in response.headers


def test_hsts_only_in_production() -> None:
    settings = Settings(_env_file=None, app_env="production")
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").headers["Strict-Transport-Security"] == HSTS


def test_unhandled_error_returns_generic_body(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("sentinel-internal-detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body == {"error": "internal_error", "request_id": response.headers["X-Request-ID"]}
    assert "sentinel-internal-detail" not in response.text
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_request_log_omits_query_string_and_raw_path(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.request"):
        response = client.get("/healthz?token=sentinel-query-value")

    [record] = [r for r in caplog.records if r.name == "app.request"]
    formatted = JsonFormatter().format(record)
    assert "sentinel-query-value" not in formatted
    entry = json.loads(formatted)
    assert entry["route"] == "/healthz"
    assert entry["status"] == 200
    assert entry["request_id"] == response.headers["X-Request-ID"]


def test_unmatched_route_logged_without_path(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.request"):
        client.get("/users/sentinel-identifier")

    [record] = [r for r in caplog.records if r.name == "app.request"]
    formatted = JsonFormatter().format(record)
    assert "sentinel-identifier" not in formatted
    assert json.loads(formatted)["route"] == "unmatched"


def test_json_formatter_includes_exception() -> None:
    try:
        raise ValueError("bad")
    except ValueError:
        record = logging.getLogger("t").makeRecord(
            "t", logging.ERROR, __file__, 1, "failed", (), exc_info=sys.exc_info()
        )

    entry = json.loads(JsonFormatter().format(record))
    assert entry["level"] == "ERROR"
    assert entry["msg"] == "failed"
    assert "ValueError" in entry["exc"]


def test_unhandled_error_log_has_type_and_request_id_only(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(settings)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("patient Marie Curie 12/05/1900")

    with (
        caplog.at_level(logging.ERROR, logger="app.main"),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.get("/boom")

    [record] = [r for r in caplog.records if r.getMessage() == "unhandled_error"]
    formatted = JsonFormatter().format(record)
    assert "Marie" not in formatted
    assert record.exc_info is None
    entry = json.loads(formatted)
    assert entry["exc_type"] == "RuntimeError"
    assert entry["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize("name", ["anthropic", "httpx", "httpx2", "httpcore", "httpcore2"])
def test_http_client_loggers_stay_at_warning_even_in_debug(name: str) -> None:
    configure_logging("DEBUG")
    try:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
        assert logging.getLogger(f"{name}._client").getEffectiveLevel() == logging.WARNING
    finally:
        configure_logging("INFO")
