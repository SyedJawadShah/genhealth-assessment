import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app import orders as orders_module
from app.db import Order
from app.main import create_app
from app.settings import Settings
from tests.conftest import VALID_NPI, FakeExtractor, Login, auth_headers

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


def test_unknown_field_names_are_not_echoed_or_logged(
    client: TestClient, login: Login, caplog: pytest.LogCaptureFixture
) -> None:
    headers = login("alice")
    with caplog.at_level(logging.INFO):
        response = client.post("/orders", json={"Marie Curie 1900": "x"}, headers=headers)

    assert response.status_code == 422
    assert response.json()["problems"] == [
        {"field": "body.<unknown field>", "problem": "extra_forbidden"}
    ]
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "Marie" not in response.text
    assert "Marie" not in logged


def test_update_increments_version(client: TestClient, login: Login) -> None:
    headers = login("alice")
    order = _create(client, headers, **COMPLETE)
    assert order["version"] == 1

    updated = client.patch(
        f"/orders/{order['id']}", json={"patient_first_name": "Augusta"}, headers=headers
    )

    assert updated.json()["version"] == 2


def test_stale_update_is_rejected_with_409(client: TestClient, login: Login) -> None:
    headers = login("alice")
    order = _create(client, headers, **COMPLETE)
    path = f"/orders/{order['id']}"
    first = client.patch(
        path, json={"patient_first_name": "Augusta"}, headers={**headers, "If-Match": '"1"'}
    )
    assert first.status_code == 200

    stale = client.patch(
        path, json={"patient_first_name": "Eve"}, headers={**headers, "If-Match": '"1"'}
    )

    assert stale.status_code == 409
    assert stale.json()["error"] == "version_conflict"
    assert client.get(path, headers=headers).json()["patient_first_name"] == "Augusta"


@pytest.mark.parametrize("value", ["2", 'W/"2"'])
def test_current_version_in_if_match_is_accepted(
    client: TestClient, login: Login, value: str
) -> None:
    headers = login("alice")
    order = _create(client, headers, **COMPLETE)
    path = f"/orders/{order['id']}"
    client.patch(path, json={"patient_first_name": "Augusta"}, headers=headers)

    response = client.patch(
        path, json={"patient_first_name": "Ada"}, headers={**headers, "If-Match": value}
    )

    assert response.status_code == 200
    assert response.json()["version"] == 3


@pytest.mark.parametrize("value", [b"abc", b"\xb2", b"1" * 11, b"", b'"-1"'])
def test_malformed_if_match_is_422(client: TestClient, login: Login, value: bytes) -> None:
    headers = login("alice")
    order = _create(client, headers, **COMPLETE)

    response = client.patch(
        f"/orders/{order['id']}",
        json={},
        headers={b"Authorization": headers["Authorization"].encode(), b"If-Match": value},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


def test_concurrent_write_between_read_and_commit_is_409(
    tmp_path: Path, extractor: FakeExtractor, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(_env_file=None, app_env="test", database_url=f"sqlite:///{tmp_path}/c.db")
    with TestClient(create_app(settings, extractor=extractor)) as client:
        headers = auth_headers(client, "alice")
        order = _create(client, headers, **COMPLETE)
        factory = client.app.state.session_factory  # type: ignore[attr-defined]  # Starlette types app as ASGIApp
        real_apply = orders_module._apply_manual

        def apply_then_race(target: Order, body: Any, names: set[str]) -> None:
            real_apply(target, body, names)
            with factory() as other:  # another request commits first
                other.execute(update(Order).values(version=Order.version + 1))
                other.commit()

        monkeypatch.setattr(orders_module, "_apply_manual", apply_then_race)
        response = client.patch(
            f"/orders/{order['id']}", json={"patient_first_name": "Eve"}, headers=headers
        )

    assert response.status_code == 409
    assert response.json()["error"] == "version_conflict"


def test_delete_racing_an_update_is_409(
    tmp_path: Path, extractor: FakeExtractor, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(_env_file=None, app_env="test", database_url=f"sqlite:///{tmp_path}/d.db")
    with TestClient(create_app(settings, extractor=extractor)) as client:
        headers = auth_headers(client, "alice")
        order = _create(client, headers, **COMPLETE)
        factory = client.app.state.session_factory  # type: ignore[attr-defined]  # Starlette types app as ASGIApp
        real_owned = orders_module._owned_order

        def owned_then_race(session: Any, order_id: str, user_id: int) -> Order:
            found = real_owned(session, order_id, user_id)
            with factory() as other:  # a PATCH commits between our read and our delete
                other.execute(update(Order).values(version=Order.version + 1))
                other.commit()
            return found

        monkeypatch.setattr(orders_module, "_owned_order", owned_then_race)
        response = client.delete(f"/orders/{order['id']}", headers=headers)

    assert response.status_code == 409
    assert response.json()["error"] == "version_conflict"
