from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.db import make_engine, make_session_factory
from app.main import create_app
from app.settings import Settings


def test_healthz_reports_status_and_version(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "test-sha"}


def test_index_escapes_configured_values() -> None:
    settings = Settings(_env_file=None, app_name="<script>x</script>", git_sha="<b>sha</b>")
    with TestClient(create_app(settings)) as client:
        body = client.get("/").text

    assert "<script>" not in body
    assert "&lt;script&gt;x&lt;/script&gt;" in body
    assert "&lt;b&gt;sha&lt;/b&gt;" in body


def test_api_docs_available_outside_production(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200


def test_api_docs_available_in_production_by_default() -> None:
    # Reviewers use Swagger UI on the deployed service to try the upload endpoint.
    settings = Settings(_env_file=None, app_env="production")
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200


def test_api_docs_can_be_disabled() -> None:
    settings = Settings(_env_file=None, app_env="production", docs_enabled=False)
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_unreachable_database_fails_startup_with_a_clear_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    url = f"sqlite:///{tmp_path}/missing-dir/app.db"
    settings = Settings(_env_file=None, app_env="test", database_url=url)

    with pytest.raises(OperationalError), TestClient(create_app(settings)):
        pass

    [record] = [r for r in caplog.records if r.getMessage() == "database_unreachable"]
    assert record.__dict__["exc_type"] == "OperationalError"
    assert "missing-dir" not in record.__dict__["detail"]


def test_readyz_ok_when_database_and_extraction_work(client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok", "extraction": "ok"}


def test_readyz_fails_without_extraction_key() -> None:
    with TestClient(create_app(Settings(_env_file=None, app_env="test"))) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database": "ok",
        "extraction": "unconfigured",
    }


def test_readyz_fails_without_database_in_production() -> None:
    with TestClient(create_app(Settings(_env_file=None, app_env="production"))) as client:
        response = client.get("/readyz")
        assert client.get("/healthz").status_code == 200

    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"


def test_readyz_fails_when_database_query_fails(client: TestClient, tmp_path: Path) -> None:
    broken = make_engine(f"sqlite:///{tmp_path}/missing-dir/app.db")
    client.app.state.session_factory = make_session_factory(broken)  # type: ignore[attr-defined]  # Starlette types app as ASGIApp

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"
