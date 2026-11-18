"""The passcode gate (passcode.py), through a small app shaped like the real ones."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

import pytest
from fastapi import Depends, FastAPI, Form, Response
from fastapi.testclient import TestClient

from appkit.passcode import PASSCODE_SECRET, PasscodeGate


@dataclass(frozen=True)
class FakeSettings:
    require_passcode: bool = True
    cookie_secret: str = ""
    cookie_max_age_seconds: int = 3600


class World:
    """What the gate reads, changeable mid-test as an app's caches would be."""

    def __init__(self) -> None:
        self.settings = FakeSettings()
        self.secrets = {PASSCODE_SECRET: "test-passcode"}

    def secret(self, name: str) -> str:
        if name not in self.secrets:
            raise RuntimeError(f"{name} is not set")
        return self.secrets[name]


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def gate(world: World) -> PasscodeGate:
    return PasscodeGate("app_session", settings=lambda: world.settings, secret=world.secret)


@pytest.fixture
def client(gate: PasscodeGate) -> TestClient:
    app = FastAPI()

    @app.post("/login")
    def login(response: Response, passcode: str = Form()) -> dict[str, bool]:
        if not gate.check_passcode(passcode):
            response.status_code = 401
            return {"ok": False}
        response.set_cookie(gate.cookie_name, gate.issue(), secure=True, httponly=True)
        return {"ok": True}

    @app.get("/", dependencies=[Depends(gate.require_session)])
    def home() -> dict[str, str]:
        return {"page": "home"}

    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def test_a_page_redirects_to_the_login_form_without_a_session(client):
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_right_passcode_opens_it(client):
    assert client.post("/login", data={"passcode": "test-passcode"}).status_code == 200
    assert client.get("/").json() == {"page": "home"}


def test_a_wrong_passcode_is_refused(client):
    assert client.post("/login", data={"passcode": "wrong"}).status_code == 401
    assert client.get("/").status_code == 303


@pytest.mark.parametrize("passcode", ["bench-100£", "⚖️-day"])
def test_a_passcode_outside_ascii_is_compared_rather_than_crashing(world, gate, passcode):
    """A `£` in the passcode used to 500 every login, right and wrong alike."""
    world.secrets[PASSCODE_SECRET] = passcode
    assert gate.check_passcode(passcode)
    assert not gate.check_passcode("wrong")
    assert not gate.check_passcode("£")


def test_an_empty_configured_passcode_opens_nothing(world, gate):
    world.secrets[PASSCODE_SECRET] = ""
    assert not gate.check_passcode("")


def test_the_gate_can_be_switched_off(world, client):
    world.settings = replace(world.settings, require_passcode=False)
    assert client.get("/").status_code == 200


def test_a_token_this_gate_issued_is_valid(gate):
    assert gate.valid(gate.issue())


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "no-dot",
        "abc.def",
        f"{int(time.time())}.forged",
        # Digits outside ASCII pass str.isdigit and used to raise on encode.
        "١٢.sig",
        # A signature outside ASCII used to raise TypeError in compare_digest.
        f"{int(time.time())}.é",
    ],
)
def test_a_token_this_gate_did_not_issue_is_refused(gate, token):
    assert not gate.valid(token)


def test_a_cookie_outside_ascii_is_refused_by_the_page_not_a_500(client):
    """Header bytes arrive decoded as latin-1, so a client can send any of them."""
    cookie = f"app_session={int(time.time())}.\xe9forged".encode("latin-1")
    assert client.get("/", headers={"cookie": cookie}).status_code == 303


def test_a_session_older_than_the_cookie_lifetime_is_refused(gate, monkeypatch):
    token = gate.issue()
    later = time.time() + 3601
    monkeypatch.setattr(time, "time", lambda: later)
    assert not gate.valid(token)


def test_rotating_the_passcode_logs_every_browser_out(world, gate):
    """The useful property of deriving the cookie key from the passcode."""
    token = gate.issue()
    world.secrets[PASSCODE_SECRET] = "a-new-passcode"
    assert not gate.valid(token)


def test_an_empty_passcode_makes_no_session_valid(world, gate):
    """sha256 of nothing is public: deriving a key from it would fail open."""
    token = gate.issue()
    world.secrets[PASSCODE_SECRET] = ""
    assert not gate.valid(token)
    with pytest.raises(ValueError, match="APP-PASSCODE is empty"):
        gate.issue()


def test_a_token_stamped_in_the_future_is_refused(gate, monkeypatch):
    real = time.time()
    monkeypatch.setattr(time, "time", lambda: real + 3600)
    token = gate.issue()
    monkeypatch.setattr(time, "time", lambda: real)
    assert not gate.valid(token)


def test_a_little_clock_skew_between_replicas_is_tolerated(gate, monkeypatch):
    real = time.time()
    monkeypatch.setattr(time, "time", lambda: real + 60)
    token = gate.issue()
    monkeypatch.setattr(time, "time", lambda: real)
    assert gate.valid(token)


def test_a_cookie_secret_decouples_the_two(world, gate):
    world.settings = replace(world.settings, cookie_secret="a-fixed-key")
    token = gate.issue()
    world.secrets[PASSCODE_SECRET] = "a-new-passcode"
    assert gate.valid(token)


def test_a_cookie_secret_needs_no_passcode_to_sign(world, gate):
    world.settings = replace(world.settings, cookie_secret="a-fixed-key")
    del world.secrets[PASSCODE_SECRET]
    assert gate.valid(gate.issue())


def test_the_login_path_is_configurable(world):
    gate = PasscodeGate(
        "s", settings=lambda: world.settings, secret=world.secret, login_path="/signin"
    )
    app = FastAPI()
    app.get("/", dependencies=[Depends(gate.require_session)])(lambda: {})
    response = TestClient(app, follow_redirects=False).get("/")
    assert response.headers["location"] == "/signin"
