from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, update

from app.auth import hash_password, verify_password
from app.db import AuthToken
from app.main import create_app
from app.settings import Settings
from tests.conftest import Login

PW = "a-long-test-password"


def _register(client: TestClient, username: str = "alice", password: str = PW) -> int:
    response = client.post("/auth/register", json={"username": username, "password": password})
    return response.status_code


def _token(client: TestClient, username: str = "alice", password: str = PW) -> str:
    response = client.post("/auth/token", data={"username": username, "password": password})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"  # noqa: S105  # OAuth2 token type, not a secret
    assert body["expires_in"] == 12 * 3600
    token: str = body["access_token"]
    return token


def test_register_login_and_me(client: TestClient) -> None:
    assert _register(client, "Alice") == 201
    token = _token(client, "ALICE")

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["username"] == "alice"


def test_password_is_not_stored_or_returned(client: TestClient) -> None:
    response = client.post("/auth/register", json={"username": "alice", "password": PW})

    assert PW not in response.text
    assert set(response.json()) == {"id", "username"}


def test_duplicate_username_is_refused(client: TestClient) -> None:
    assert _register(client) == 201
    response = client.post("/auth/register", json={"username": "ALICE", "password": PW})

    assert response.status_code == 409
    assert response.json()["error"] == "username_taken"


@pytest.mark.parametrize(
    ("username", "password"),
    [("al", PW), ("alice smith", PW), ("a" * 33, PW), ("alice", "short")],
)
def test_register_validates_input(client: TestClient, username: str, password: str) -> None:
    assert _register(client, username, password) == 422


@pytest.mark.parametrize(
    ("username", "password"), [("alice", "wrong-password-here"), ("nobody", PW)]
)
def test_bad_credentials_get_the_same_401(client: TestClient, username: str, password: str) -> None:
    _register(client)
    response = client.post("/auth/token", data={"username": username, "password": password})

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_credentials"
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("header", [None, "Bearer not-a-real-token", "Basic abc"])
def test_protected_routes_need_a_valid_token(client: TestClient, header: str | None) -> None:
    headers = {"Authorization": header} if header else {}
    for method, path in [("GET", "/auth/me"), ("GET", "/orders"), ("POST", "/orders/upload")]:
        response = client.request(method, path, headers=headers)
        assert response.status_code == 401, path
        assert response.json()["error"] == "not_authenticated"


def test_expired_token_is_refused(client: TestClient) -> None:
    _register(client)
    token = _token(client)
    factory = client.app.state.session_factory  # type: ignore[attr-defined]  # Starlette types app as ASGIApp
    with factory() as session:
        session.execute(update(AuthToken).values(expires_at=datetime(2020, 1, 1, tzinfo=UTC)))
        session.commit()

    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_logout_revokes_only_that_token(client: TestClient) -> None:
    _register(client)
    first, second = _token(client), _token(client)

    response = client.post("/auth/logout", headers={"Authorization": f"Bearer {first}"})

    assert response.status_code == 204
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {first}"}).status_code == 401
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {second}"}).status_code == 200


def test_tokens_are_stored_as_digests(client: TestClient) -> None:
    _register(client)
    token = _token(client)
    factory = client.app.state.session_factory  # type: ignore[attr-defined]  # Starlette types app as ASGIApp
    with factory() as session:
        digests = list(session.scalars(select(AuthToken.token_digest)))

    assert len(digests) == 1
    assert token not in digests[0]


def test_login_is_rate_limited() -> None:
    settings = Settings(_env_file=None, app_env="test", login_limit_per_minute=3)
    with TestClient(create_app(settings)) as client:
        codes = [
            client.post("/auth/token", data={"username": "x", "password": "y"}).status_code
            for _ in range(4)
        ]
        response = client.post("/auth/token", data={"username": "x", "password": "y"})

    assert codes == [401, 401, 401, 429]
    assert response.json()["error"] == "rate_limited"
    assert int(response.headers["Retry-After"]) > 0


@pytest.mark.parametrize(
    "form", [{"username": "u" * 255, "password": PW}, {"username": "alice", "password": "p" * 1025}]
)
def test_login_rejects_oversized_credentials_before_limits(
    client: TestClient, form: dict[str, str]
) -> None:
    response = client.post("/auth/token", data=form)

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"
    limiter = client.app.state.rate_limiter  # type: ignore[attr-defined]  # Starlette types app as ASGIApp
    assert limiter._windows == {}


def test_registration_is_rate_limited() -> None:
    settings = Settings(_env_file=None, app_env="test", register_limit_per_hour=2)
    with TestClient(create_app(settings)) as client:
        codes = [_register(client, f"user{i}") for i in range(3)]

    assert codes == [201, 201, 429]


def test_demo_users_are_seeded_and_password_reset(tmp_path: object) -> None:
    url = f"sqlite:///{tmp_path}/demo.db"
    users = {"Demo-Alice": SecretStr("first-password-1"), "demo-bob": SecretStr("bob-password-1")}
    settings = Settings(_env_file=None, app_env="test", database_url=url, demo_users=users)
    with TestClient(create_app(settings)) as client:
        assert _token(client, "demo-alice", "first-password-1")
        assert _token(client, "demo-bob", "bob-password-1")

    users["Demo-Alice"] = SecretStr("second-password-2")
    settings = Settings(_env_file=None, app_env="test", database_url=url, demo_users=users)
    with TestClient(create_app(settings)) as client:
        assert _token(client, "demo-alice", "second-password-2")
        bad = {"username": "demo-alice", "password": "first-password-1"}
        assert client.post("/auth/token", data=bad).status_code == 401


def test_production_without_database_or_secret_refuses_instead_of_guessing() -> None:
    settings = Settings(_env_file=None, app_env="production")
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
        response = client.post("/auth/register", json={"username": "alice", "password": PW})
        assert response.status_code == 503
        assert response.json()["error"] == "service_unavailable"


def test_production_with_database_but_no_secret_refuses_login(tmp_path: object) -> None:
    settings = Settings(
        _env_file=None, app_env="production", database_url=f"sqlite:///{tmp_path}/p.db"
    )
    with TestClient(create_app(settings)) as client:
        assert _register(client) == 201
        response = client.post("/auth/token", data={"username": "alice", "password": PW})

    assert response.status_code == 503


def test_secret_key_signs_tokens_across_restarts(tmp_path: object) -> None:
    url = f"sqlite:///{tmp_path}/k.db"
    settings = Settings(_env_file=None, app_env="test", database_url=url, secret_key="k" * 32)
    with TestClient(create_app(settings)) as client:
        _register(client)
        token = _token(client)
    with TestClient(create_app(settings)) as client:
        assert (
            client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        )


def test_password_hash_round_trip_and_bad_stored_hash() -> None:
    stored = hash_password(PW)

    assert stored.startswith("scrypt$")
    assert verify_password(PW, stored)
    assert not verify_password("other-password", stored)
    assert not verify_password(PW, "not-a-hash")


def test_login_fixture_users_are_isolated(login: Login) -> None:
    assert login("carol") != login("dave")
