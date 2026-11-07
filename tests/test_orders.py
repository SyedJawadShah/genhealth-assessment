from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import VALID_NPI, Login

COMPLETE = {
    "patient_first_name": "Ada",
    "patient_last_name": "Lovelace",
    "patient_date_of_birth": "1915-12-10",
    "provider_name": "Dr. Grace Hopper MD",
    "provider_npi": VALID_NPI,
}


def _create(client: TestClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    response = client.post("/orders", json=body, headers=headers)
    assert response.status_code == 201, response.text
    order: dict[str, Any] = response.json()
    return order


def test_create_complete_order_is_ready(client: TestClient, login: Login) -> None:
    order = _create(client, login("alice"), **COMPLETE)

    assert order["status"] == "ready"
    assert order["source"] == "manual"
    assert order["provider_name"] == "Grace Hopper"
    assert order["patient_date_of_birth"] == "1915-12-10"
    assert {f["status"] for f in order["fields"].values()} == {"valid"}


def test_create_partial_order_needs_review(client: TestClient, login: Login) -> None:
    order = _create(client, login("alice"), patient_last_name="Lovelace")

    assert order["status"] == "needs_review"
    assert order["patient_first_name"] is None
    assert order["fields"]["patient_first_name"] == {
        "status": "missing",
        "reason": "not_provided",
        "source": "manual",
        "extracted_text": None,
        "page": None,
    }
    assert order["fields"]["patient_last_name"]["status"] == "valid"


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("provider_npi", "12345", "not_10_digits"),
        ("patient_date_of_birth", "2999-01-01", "in_the_future"),
        ("patient_first_name", "R2-D2", "unexpected_characters"),
    ],
)
def test_create_rejects_invalid_fields_without_echoing_them(
    client: TestClient, login: Login, field: str, value: str, problem: str
) -> None:
    headers = login("alice")
    response = client.post("/orders", json={**COMPLETE, field: value}, headers=headers)

    assert response.status_code == 422
    assert response.json()["problems"] == [{"field": f"body.{field}", "problem": problem}]
    assert value not in response.text
    assert client.get("/orders", headers=headers).json() == []


def test_manual_npi_failing_checksum_is_saved_with_a_warning(
    client: TestClient, login: Login
) -> None:
    order = _create(client, login("alice"), **{**COMPLETE, "provider_npi": "1234567890"})

    assert order["provider_npi"] == "1234567890"
    assert order["status"] == "needs_review"
    assert order["fields"]["provider_npi"]["status"] == "invalid_checksum"
    assert [w["code"] for w in order["warnings"]] == ["invalid_checksum"]


def test_create_rejects_unknown_fields(client: TestClient, login: Login) -> None:
    response = client.post("/orders", json={"owner_id": 2}, headers=login("alice"))

    assert response.status_code == 422


def test_get_list_update_delete(client: TestClient, login: Login) -> None:
    headers = login("alice")
    first = _create(client, headers, patient_last_name="Lovelace")
    second = _create(client, headers, **COMPLETE)

    assert client.get(f"/orders/{first['id']}", headers=headers).json() == first
    listed = client.get("/orders", headers=headers).json()
    assert {o["id"] for o in listed} == {first["id"], second["id"]}
    assert len(client.get("/orders?limit=1", headers=headers).json()) == 1
    assert len(client.get("/orders?limit=1&offset=1", headers=headers).json()) == 1

    patch = {k: v for k, v in COMPLETE.items() if k != "patient_last_name"}
    updated = client.patch(f"/orders/{first['id']}", json=patch, headers=headers).json()
    assert updated["status"] == "ready"
    assert updated["patient_last_name"] == "Lovelace"

    cleared = client.patch(f"/orders/{first['id']}", json={"provider_npi": None}, headers=headers)
    assert cleared.json()["status"] == "needs_review"
    assert cleared.json()["fields"]["provider_npi"]["status"] == "missing"

    assert client.delete(f"/orders/{first['id']}", headers=headers).status_code == 204
    assert client.get(f"/orders/{first['id']}", headers=headers).status_code == 404
    assert [o["id"] for o in client.get("/orders", headers=headers).json()] == [second["id"]]


def test_invalid_patch_changes_nothing(client: TestClient, login: Login) -> None:
    headers = login("alice")
    order = _create(client, headers, **COMPLETE)

    response = client.patch(
        f"/orders/{order['id']}",
        json={"provider_npi": "12345", "patient_first_name": "Augusta"},
        headers=headers,
    )

    assert response.status_code == 422
    assert client.get(f"/orders/{order['id']}", headers=headers).json() == order


def test_users_cannot_see_or_change_each_others_orders(client: TestClient, login: Login) -> None:
    alice, bob = login("alice"), login("bob")
    order = _create(client, alice, **COMPLETE)
    path = f"/orders/{order['id']}"

    assert client.get("/orders", headers=bob).json() == []
    for response in [
        client.get(path, headers=bob),
        client.patch(path, json={"patient_first_name": "Eve"}, headers=bob),
        client.delete(path, headers=bob),
    ]:
        assert response.status_code == 404
        assert response.json()["error"] == "order_not_found"
    assert client.get(path, headers=alice).json() == order


def test_unknown_order_is_404(client: TestClient, login: Login) -> None:
    response = client.get("/orders/does-not-exist", headers=login("alice"))

    assert response.status_code == 404
    assert response.json() == {
        "error": "order_not_found",
        "request_id": response.headers["X-Request-ID"],
    }


def test_list_rejects_bad_paging(client: TestClient, login: Login) -> None:
    assert client.get("/orders?limit=0", headers=login("alice")).status_code == 422
