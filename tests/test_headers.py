"""Security headers (headers.py) on every response, errors included."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from appkit import headers


def _app() -> FastAPI:
    app = FastAPI()
    headers.install(app)

    @app.get("/", response_class=HTMLResponse)
    def page(request: Request) -> str:
        return f'<script nonce="{request.state.csp_nonce}">1</script>'

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


def _nonce(response) -> str:
    match = re.search(
        r"script-src 'self' 'nonce-([^']+)'", response.headers["content-security-policy"]
    )
    assert match
    return match.group(1)


def test_a_page_carries_every_security_header(client):
    response = client.get("/")
    for name, value in headers.STATIC_HEADERS.items():
        assert response.headers[name] == value
    policy = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in policy
    assert "object-src 'none'" in policy


def test_the_page_can_use_this_responses_nonce(client):
    response = client.get("/")
    assert f'nonce="{_nonce(response)}"' in response.text


def test_the_nonce_is_fresh_per_response(client):
    assert _nonce(client.get("/")) != _nonce(client.get("/"))


def test_a_not_found_carries_the_headers(client):
    response = client.get("/nowhere")
    assert response.status_code == 404
    assert response.headers["x-frame-options"] == "DENY"


def test_an_unhandled_error_still_carries_the_headers(client):
    """ServerErrorMiddleware would otherwise send a bare 500."""
    response = client.get("/boom")
    assert response.status_code == 500
    assert response.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in response.headers


def test_form_action_defaults_to_self(client):
    assert "form-action 'self';" in client.get("/").headers["content-security-policy"]


def test_csp_form_action_adds_redirect_origins(monkeypatch):
    """Behind Cloudflare Access a POST can bounce to the team domain."""
    monkeypatch.setenv("CSP_FORM_ACTION", "https://team.cloudflareaccess.com")
    policy = TestClient(_app()).get("/").headers["content-security-policy"]
    assert "form-action 'self' https://team.cloudflareaccess.com;" in policy
