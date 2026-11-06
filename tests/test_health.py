from fastapi.testclient import TestClient

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


def test_api_docs_disabled_in_production() -> None:
    settings = Settings(_env_file=None, app_env="production")
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
