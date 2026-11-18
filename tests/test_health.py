"""appkit.health: liveness from memory, readiness through the app's own check."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from appkit import health


def _client(ready: Callable[[], object]) -> TestClient:
    app = FastAPI()
    health.install(app, environment="dev", ready=ready)
    return TestClient(app)


def test_healthz_reports_the_image_tag_as_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_TAG", "v1.2.3")
    response = _client(lambda: None).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "environment": "dev", "version": "v1.2.3"}


def test_healthz_without_an_image_tag_says_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMAGE_TAG", raising=False)
    assert _client(lambda: None).get("/healthz").json()["version"] == "unknown"


def test_healthz_never_calls_ready() -> None:
    """Liveness must not touch storage, or a blob blip becomes a restart loop."""

    def ready() -> None:
        raise AssertionError("healthz reached storage")

    assert _client(ready).get("/healthz").status_code == 200


def test_readyz_is_ok_when_ready_returns() -> None:
    calls: list[int] = []
    client = _client(lambda: calls.append(1))
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "storage": True}
    assert calls == [1]


def test_readyz_is_503_without_the_error_when_ready_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def ready() -> None:
        raise OSError("stsecret.blob.core.windows.net unreachable")

    response = _client(ready).get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "storage": False}
    assert "stsecret" not in response.text
    assert "readiness check failed" in caplog.text
