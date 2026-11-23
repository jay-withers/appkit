"""CSRF defences (csrf.py): the origin check and the login form's token."""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from appkit import csrf, headers


def _app() -> FastAPI:
    app = FastAPI()
    csrf.install(app)
    headers.install(app)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> str:
        return f'<form><input type="hidden" name="csrf_token" value="{csrf.token(request)}"></form>'

    @app.post("/login")
    def login(request: Request, csrf_token: Annotated[str, Form()] = "") -> dict[str, bool]:
        return {"valid": csrf.valid(request, csrf_token)}

    @app.get("/plain")
    def plain() -> dict[str, str]:
        return {"page": "plain"}

    @app.post("/save")
    def save() -> dict[str, bool]:
        return {"saved": True}

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app(), base_url="https://testserver")


# --- the origin check ---------------------------------------------------------


@pytest.mark.parametrize("site", ["cross-site", "same-site", "Cross-Site"])
def test_a_cross_site_post_is_refused(client, site):
    response = client.post("/save", headers={"Sec-Fetch-Site": site})
    assert response.status_code == 403
    # Refused inside the headers middleware, so the refusal is covered too.
    assert "content-security-policy" in response.headers


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_a_same_origin_or_user_started_post_passes(client, site):
    assert client.post("/save", headers={"Sec-Fetch-Site": site}).json() == {"saved": True}


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_every_unsafe_method_is_checked(client, method):
    response = client.request(method, "/save", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403


def test_a_cross_site_get_is_not_refused(client):
    assert client.get("/plain", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_sec_fetch_site_wins_over_origin(client):
    response = client.post(
        "/save", headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://testserver"}
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("origin", "status"),
    [
        ("https://testserver", 200),
        ("https://TestServer", 200),
        ("https://evil.example", 403),
        ("https://testserver.evil.example", 403),
        ("null", 403),
    ],
)
def test_without_sec_fetch_site_origin_is_compared_with_host(client, origin, status):
    assert client.post("/save", headers={"Origin": origin}).status_code == status


@pytest.mark.parametrize(
    ("host", "origin", "status"),
    [
        ("app.example:443", "https://app.example", 200),
        ("app.example", "https://app.example:443", 200),
        ("app.example:8443", "https://app.example", 403),
        ("localhost:8000", "http://localhost:8000", 200),
    ],
)
def test_a_default_port_spelled_out_on_one_side_still_matches(client, host, origin, status):
    response = client.post("/save", headers={"Host": host, "Origin": origin})
    assert response.status_code == status


def test_a_request_from_no_browser_passes(client):
    """No Sec-Fetch-Site and no Origin: curl, a webhook, a test client."""
    assert client.post("/save").json() == {"saved": True}


# --- the form token -----------------------------------------------------------


def test_the_form_round_trips(client):
    page = client.get("/login")
    assert page.cookies.get(csrf.COOKIE_NAME) == csrf.token_in(page.text)
    response = client.post("/login", data={"csrf_token": csrf.token_in(page.text)})
    assert response.json() == {"valid": True}


def test_the_cookie_is_host_only_secure_and_http_only(client):
    set_cookie = client.get("/login").headers["set-cookie"]
    assert set_cookie.startswith(f"{csrf.COOKIE_NAME}=")
    for attribute in ("Path=/", "Secure", "HttpOnly", "SameSite=lax"):
        assert attribute in set_cookie
    assert "Domain" not in set_cookie


def test_a_page_without_a_form_sets_no_cookie(client):
    assert "set-cookie" not in client.get("/plain").headers


def test_a_browser_keeps_its_token_across_pages(client):
    first = csrf.token_in(client.get("/login").text)
    again = client.get("/login")
    assert csrf.token_in(again.text) == first
    assert "set-cookie" not in again.headers


def test_a_malformed_cookie_is_replaced_not_echoed(client):
    client.cookies.set(csrf.COOKIE_NAME, '"><script>')
    page = client.get("/login")
    assert "<script>" not in page.text
    assert page.cookies.get(csrf.COOKIE_NAME) == csrf.token_in(page.text)


@pytest.mark.parametrize("presented", ["", "wrong", "£-not-ascii"])
def test_a_wrong_or_missing_token_is_invalid(client, presented):
    client.get("/login")
    assert client.post("/login", data={"csrf_token": presented}).json() == {"valid": False}


def test_a_token_without_its_cookie_is_invalid(client):
    """The forger's case: a token it fetched itself, from a browser that has none."""
    token = csrf.token_in(client.get("/login").text)
    client.cookies.clear()
    assert client.post("/login", data={"csrf_token": token}).json() == {"valid": False}


@pytest.mark.parametrize(
    "markup",
    [
        '<input type="hidden" name="csrf_token" value="abc">',
        '<input value="abc" type="hidden" name="csrf_token">',
        "<input type='hidden' name='csrf_token' id='t' value='abc'>",
    ],
)
def test_token_in_reads_the_field_however_its_attributes_are_written(markup):
    assert csrf.token_in(f"<form>{markup}</form>") == "abc"


def test_token_in_ignores_other_fields_values():
    page = '<input name="passcode" value="no"><input name="csrf_token" value="yes">'
    assert csrf.token_in(page) == "yes"


def test_token_in_says_when_the_page_has_no_field():
    with pytest.raises(ValueError, match="csrf_token"):
        csrf.token_in("<form></form>")
