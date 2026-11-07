import hashlib
import logging
from pathlib import Path

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


def test_upload_keeps_flagged_npi_and_nulls_only_missing_fields(
    client: TestClient, login: Login, extractor: FakeExtractor
) -> None:
    # Mirrors the sample fax: an NPI that fails its check digit is kept and flagged; a field
    # that is not in the document is null.
    extractor.result = extracted(provider_npi="9182734556", patient_date_of_birth=None)

    status, order = _upload(client, login("alice"))

    assert status == 201
    assert order["status"] == "needs_review"
    assert order["provider_npi"] == "9182734556"
    assert order["patient_date_of_birth"] is None
    assert order["warnings"] == [
        {
            "field": "provider_npi",
            "code": "invalid_checksum",
            "message": "NPI fails the check digit (Luhn with prefix 80840). "
            "Stored as extracted; verify it before use.",
        }
    ]
    fields = order["fields"]
    assert isinstance(fields, dict)
    assert fields["provider_npi"] == {
        "status": "invalid_checksum",
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
    assert response.json()["warnings"] == []
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


FIXTURE_3_PAGES = (Path(__file__).parent / "fixtures" / "multipage_split.pdf").read_bytes()


@pytest.mark.parametrize(
    ("data", "page"), [(FIXTURE_3_PAGES, 4), (FIXTURE_3_PAGES, 0), (FAKE_PDF, 0)]
)
def test_cited_page_outside_document_needs_review(
    client: TestClient, login: Login, extractor: FakeExtractor, data: bytes, page: int
) -> None:
    result = extracted()
    result.provider_npi.page = page
    extractor.result = result

    code, order = _upload(client, login("alice"), data=data)

    assert code == 201
    assert order["status"] == "needs_review"
    assert order["provider_npi"] == VALID_NPI
    fields = order["fields"]
    assert isinstance(fields, dict)
    assert fields["provider_npi"]["status"] == "needs_review"
    assert fields["provider_npi"]["reason"] == "page_out_of_range"
    assert fields["provider_npi"]["page"] is None
    assert fields["patient_first_name"]["status"] == "valid"
    warnings = order["warnings"]
    assert isinstance(warnings, list)
    assert [w["code"] for w in warnings] == ["needs_review"]


def test_cited_page_within_document_is_kept(
    client: TestClient, login: Login, extractor: FakeExtractor
) -> None:
    result = extracted()
    result.provider_npi.page = 3
    extractor.result = result

    _, order = _upload(client, login("alice"), data=FIXTURE_3_PAGES)

    assert order["status"] == "ready"


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("not_pdf.txt", "text/plain"),
        ("not_pdf.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("not_pdf.png", "image/png"),
        ("text_renamed.pdf", "application/pdf"),
    ],
)
def test_non_pdf_uploads_get_a_clear_415(
    client: TestClient, login: Login, extractor: FakeExtractor, name: str, content_type: str
) -> None:
    response = client.post(
        "/orders/upload",
        files={"file": (name, (FIXTURES / name).read_bytes(), content_type)},
        headers=login("alice"),
    )

    assert response.status_code == 415
    assert response.json()["error"] == "not_a_pdf"
    assert response.json()["message"] == "Only PDF files are accepted."
    assert extractor.calls == 0


def test_real_pdf_is_accepted_whatever_its_name(client: TestClient, login: Login) -> None:
    response = client.post(
        "/orders/upload",
        files={"file": ("scan.png", (FIXTURES / "multipage_split.pdf").read_bytes(), "image/png")},
        headers=login("alice"),
    )

    assert response.status_code == 201
