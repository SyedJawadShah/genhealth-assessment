import re

from fastapi.testclient import TestClient

UNSAFE_SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(")


def test_frontend_page_is_served(client: TestClient) -> None:
    response = client.get("/app/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<script src="app.js" defer></script>' in response.text
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_frontend_page_has_no_inline_script_or_style(client: TestClient) -> None:
    html = client.get("/app/").text

    # The Content-Security-Policy blocks inline code, so everything must come from files.
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "<style" not in html
    assert " style=" not in html
    assert not re.search(r"<[^>]+\son[a-z]+\s*=", html)  # no inline event handlers


def test_frontend_assets_have_correct_types(client: TestClient) -> None:
    script = client.get("/app/app.js")
    styles = client.get("/app/app.css")

    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert styles.status_code == 200
    assert styles.headers["content-type"].startswith("text/css")


def test_frontend_script_never_renders_html_from_data(client: TestClient) -> None:
    script = client.get("/app/app.js").text

    for sink in UNSAFE_SINKS:
        assert sink not in script


def test_bare_app_path_redirects_to_the_page(client: TestClient) -> None:
    response = client.get("/app", follow_redirects=False)

    assert response.status_code in (307, 308)
    assert response.headers["location"].endswith("/app/")


def test_hidden_sections_stay_hidden(client: TestClient) -> None:
    # Layout rules like `.grid { display: grid }` would otherwise override `hidden`.
    styles = client.get("/app/app.css").text

    assert "[hidden] { display: none !important; }" in styles


def test_registration_form_states_the_server_rules(client: TestClient) -> None:
    html = client.get("/app/").text

    assert 'minlength="3" maxlength="32"' in html
    assert 'minlength="12" maxlength="128"' in html
