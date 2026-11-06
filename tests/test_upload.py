import hashlib
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import Order
from app.extraction import ExtractionError
from app.main import create_app
from app.settings import Settings
from tests.conftest import FAKE_PDF, VALID_NPI, FakeExtractor, Login, auth_headers, extracted


def _upload(
    client: TestClient, headers: dict[str, str], data: bytes = FAKE_PDF, name: str = "order.pdf"
) -> tuple[int, dict[str, object]]:
    response = client.post(
        "/orders/upload", files={"file": (name, data, "application/pdf")}, headers=headers
    )
    body: dict[str, object] = response.json()
    return response.status_code, body


def test_upload_creates_a_ready_order(client: TestClient, login: Login) -> None:
    headers = login("alice")
    status, order = _upload(client, headers)

    assert status == 201
    assert order["status"] == "ready"
    assert order["source"] == "upload"
    assert order["patient_first_name"] == "Ada"
    assert order["patient_date_of_birth"] == "1915-12-10"
    assert order["provider_name"] == "Grace Hopper"
    assert order["provider_npi"] == VALID_NPI
    assert client.get(f"/orders/{order['id']}", headers=headers).json() == order


def test_upload_keeps_failed_and_missing_fields_null_with_reasons(
    client: TestClient, login: Login, extractor: FakeExtractor
) -> None:
    # Mirrors the sample fax: an NPI that fails its check digit, plus a field not found.
    extractor.result = extracted(provider_npi="9182734556", patient_date_of_birth=None)

    status, order = _upload(client, login("alice"))

    assert status == 201
    assert order["status"] == "needs_review"
    assert order["provider_npi"] is None
    assert order["patient_date_of_birth"] is None
    fields = order["fields"]
    assert isinstance(fields, dict)
    assert fields["provider_npi"] == {
        "status": "invalid",
        "reason": "checksum_failed",
        "source": "extracted",
        "extracted_text": "9182734556",
        "page": 1,
    }
    assert fields["patient_date_of_birth"] == {
        "status": "missing",
        "reason": "not_found_in_document",
        "source": "extracted",
        "extracted_text": None,
        "page": None,
    }


def test_upload_then_correct_by_patch(
    client: TestClient, login: Login, extractor: FakeExtractor
) -> None:
    headers = login("alice")
    extractor.result = extracted(provider_npi="9182734556")
    _, order = _upload(client, headers)

    response = client.patch(
        f"/orders/{order['id']}", json={"provider_npi": VALID_NPI}, headers=headers
    )

    assert response.json()["status"] == "ready"
    assert response.json()["fields"]["provider_npi"]["source"] == "manual"


def test_upload_stores_document_hash_not_filename(client: TestClient, login: Login) -> None:
    _, order = _upload(client, login("alice"), name="../../etc/Marie Curie DOB 1900.pdf")

    factory = client.app.state.session_factory  # type: ignore[attr-defined]  # Starlette types app as ASGIApp
    with factory() as session:
        stored = session.scalar(select(Order).where(Order.id == order["id"]))
    assert stored is not None
    assert stored.document_sha256 == hashlib.sha256(FAKE_PDF).hexdigest()


@pytest.mark.parametrize(
    ("data", "status", "error"),
    [
        (b"", 422, "empty_file"),
        (b"PK\x03\x04 a zip renamed to .pdf", 415, "not_a_pdf"),
        (b" " * 2000 + b"%PDF-1.4", 415, "not_a_pdf"),
    ],
)
def test_upload_checks_content_not_name(
    client: TestClient,
    login: Login,
    extractor: FakeExtractor,
    data: bytes,
    status: int,
    error: str,
) -> None:
    code, body = _upload(client, login("alice"), data=data)

    assert code == status
    assert body["error"] == error
    assert extractor.calls == 0


def test_upload_rejects_oversized_file(extractor: FakeExtractor) -> None:
    settings = Settings(_env_file=None, app_env="test", max_upload_bytes=len(FAKE_PDF) - 1)
    with TestClient(create_app(settings, extractor=extractor)) as client:
        code, body = _upload(client, auth_headers(client, "alice"))

    assert (code, body["error"]) == (413, "file_too_large")
    assert extractor.calls == 0


def test_upload_requires_a_file(client: TestClient, login: Login) -> None:
    response = client.post("/orders/upload", headers=login("alice"))

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ExtractionError("document_unreadable", 422), 422),
        (ExtractionError("extraction_failed", 502), 502),
        (ExtractionError("extraction_unavailable", 503), 503),
    ],
)
def test_extraction_failure_saves_nothing(
    client: TestClient, login: Login, extractor: FakeExtractor, error: ExtractionError, status: int
) -> None:
    headers = login("alice")
    extractor.result = error

    code, body = _upload(client, headers)

    assert (code, body["error"]) == (status, error.code)
    assert client.get("/orders", headers=headers).json() == []


def test_upload_is_rate_limited_per_user(extractor: FakeExtractor) -> None:
    settings = Settings(_env_file=None, app_env="test", upload_limit_per_hour=2)
    with TestClient(create_app(settings, extractor=extractor)) as client:
        alice_headers = auth_headers(client, "alice")
        alice = [_upload(client, alice_headers)[0] for _ in range(3)]
        bob = _upload(client, auth_headers(client, "bob"))[0]

    assert alice == [201, 201, 429]
    assert bob == 201
    assert extractor.calls == 3


def test_upload_logs_no_personal_data(
    client: TestClient, login: Login, caplog: pytest.LogCaptureFixture
) -> None:
    headers = login("alice")
    with caplog.at_level(logging.DEBUG):
        _upload(client, headers)

    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for value in ("Ada", "Lovelace", "1915", "Hopper", VALID_NPI):
        assert value not in logged
    assert "order_created" in logged


def test_unconfigured_extractor_answers_503() -> None:
    settings = Settings(_env_file=None, app_env="test")
    with TestClient(create_app(settings)) as client:
        code, body = _upload(client, auth_headers(client, "alice"))

    assert (code, body["error"]) == (503, "extraction_unavailable")
