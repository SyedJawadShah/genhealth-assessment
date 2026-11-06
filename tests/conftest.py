from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.extraction import ExtractedField, ExtractedOrder, ExtractionError
from app.main import create_app
from app.settings import Settings

# Synthetic data only. 1234567893 is the example NPI from the CMS NPI check-digit guide.
VALID_NPI = "1234567893"
FAKE_PDF = b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"


def extracted(**values: str | None) -> ExtractedOrder:
    defaults: dict[str, str | None] = {
        "patient_first_name": "Ada",
        "patient_last_name": "Lovelace",
        "patient_date_of_birth": "12/10/1915",
        "provider_name": "Grace Hopper, MD",
        "provider_npi": VALID_NPI,
    }
    defaults.update(values)
    return ExtractedOrder(
        **{name: ExtractedField(value=value, page=1) for name, value in defaults.items()}
    )


class FakeExtractor:
    def __init__(self) -> None:
        self.result: ExtractedOrder | ExtractionError = extracted()
        self.calls = 0

    def extract(self, pdf: bytes) -> ExtractedOrder:
        assert pdf.startswith(b"%PDF-")
        self.calls += 1
        if isinstance(self.result, ExtractionError):
            raise self.result
        return self.result


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, app_env="test", git_sha="test-sha")


@pytest.fixture
def extractor() -> FakeExtractor:
    return FakeExtractor()


@pytest.fixture
def client(settings: Settings, extractor: FakeExtractor) -> Iterator[TestClient]:
    with TestClient(create_app(settings, extractor=extractor)) as test_client:
        yield test_client


def auth_headers(client: TestClient, username: str) -> dict[str, str]:
    """Register a user (if needed) and return bearer-token headers for them."""
    password = f"{username}-correct-horse"
    client.post("/auth/register", json={"username": username, "password": password})
    response = client.post("/auth/token", data={"username": username, "password": password})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


Login = Callable[[str], dict[str, str]]


@pytest.fixture
def login(client: TestClient) -> Login:
    return lambda username: auth_headers(client, username)
