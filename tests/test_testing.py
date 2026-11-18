"""appkit.testing: the live server, and the browser helpers against a real Chromium."""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import BrowserType, Page

from appkit import testing
from tests.e2e_app.app import PASSCODE

ROOT = Path(__file__).resolve().parents[1]


def _get(url: str) -> int:
    with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - loopback
        status: int = response.status
        return status


# --- the server, no browser ---------------------------------------------------


def test_free_port_is_bindable() -> None:
    port = testing.free_port()
    assert 1024 <= port <= 65535


def test_console_script_sits_beside_the_interpreter() -> None:
    assert Path(testing.console_script("uvicorn")).parent == Path(sys.executable).parent


def test_live_server_yields_a_url_that_answers_and_stops_it_after() -> None:
    argv = [sys.executable, "-m", "http.server", "{port}", "--bind", "{host}"]
    with testing.live_server(argv, health_path="/") as url:
        assert url.startswith(f"http://{testing.HOST}:")
        assert _get(url + "/") == 200
    with pytest.raises(urllib.error.URLError):
        _get(url + "/")


def test_live_server_passes_env_through() -> None:
    script = (
        "import http.server, os, sys\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200 if os.environ['E2E_MARK'] == 'yes' else 500)\n"
        "        self.end_headers()\n"
        "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()\n"
    )
    with testing.live_server([sys.executable, "-c", script, "{port}"], env={"E2E_MARK": "yes"}):
        pass


def test_env_none_removes_a_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("E2E_GONE", "still here")
    script = (
        "import http.server, os, sys\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(500 if 'E2E_GONE' in os.environ else 200)\n"
        "        self.end_headers()\n"
        "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()\n"
    )
    with testing.live_server(
        [sys.executable, "-c", script, "{port}"], env={"E2E_GONE": None}, timeout=5
    ):
        pass


def test_a_child_the_server_started_is_stopped_with_it(tmp_path: Path) -> None:
    """A reloader or wrapper's child must not outlive the block holding the port."""
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "import runpy; sys.argv = ['http.server', sys.argv[1], '--bind', '127.0.0.1']\n"
        "runpy.run_module('http.server', run_name='__main__')\n"
    )
    with testing.live_server([sys.executable, "-c", script, "{port}"], health_path="/"):
        child = int(pid_file.read_text())
        os.kill(child, 0)  # alive while the server is
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"child {child} outlived the server")


def test_a_server_that_dies_reports_its_own_output() -> None:
    argv = [sys.executable, "-c", "print('import went wrong'); raise SystemExit(3)"]
    with (
        pytest.raises(RuntimeError, match="exited with 3") as caught,
        testing.live_server(argv),
    ):
        pass
    assert "import went wrong" in str(caught.value)


def test_a_server_that_never_gets_healthy_times_out() -> None:
    # Serves, but 404s the health path.
    argv = [sys.executable, "-m", "http.server", "{port}", "--bind", "{host}"]
    with (
        pytest.raises(RuntimeError, match="did not answer 200 within 1s"),
        testing.live_server(argv, health_path="/not-here", timeout=1),
    ):
        pass


# --- the browser helpers -------------------------------------------------------


@pytest.fixture(scope="session")
def browser_type(browser_type: BrowserType) -> BrowserType:
    """pytest-playwright's, checked before its `browser` fixture launches."""
    testing.require_browser(browser_type)
    return browser_type


@pytest.fixture(scope="session")
def base_url() -> Iterator[str]:
    """pytest-playwright's base URL: the e2e app, served for the session."""
    argv = [
        testing.console_script("uvicorn"),
        "tests.e2e_app.app:app",
        "--host",
        "{host}",
        "--port",
        "{port}",
    ]
    # Started before conftest's per-test `isolated` clears these, so dropped
    # here: a developer's CSP_FORM_ACTION would otherwise block POST /login.
    cleared: dict[str, str | None] = {
        name: None
        for name in ("KEY_VAULT_URI", "AZURE_CLIENT_ID", "AZURE_KEYVAULT_TOKEN", "CSP_FORM_ACTION")
    }
    with testing.live_server(argv, env={"PYTHONPATH": str(ROOT), **cleared}) as url:
        yield url


def test_log_in_reaches_the_gated_page_with_the_secure_cookie(page: Page) -> None:
    errors = testing.watch_console(page)
    testing.log_in(page, PASSCODE)
    assert page.inner_text("h1") == "in"
    assert errors == []


def test_log_in_with_the_wrong_passcode_fails_at_the_login_step(page: Page) -> None:
    with pytest.raises(Exception, match="Timeout"):
        page.set_default_timeout(1500)
        testing.log_in(page, "wrong")
    assert page.url.endswith("/login")


def test_watch_console_catches_a_script_the_csp_blocks(page: Page) -> None:
    errors = testing.watch_console(page)
    page.goto("/no-nonce")
    page.wait_for_load_state()
    assert page.title() == "bad"  # the inline script did not run
    assert any("Content Security Policy" in error for error in errors), errors


def test_watch_console_catches_an_uncaught_exception(page: Page) -> None:
    errors = testing.watch_console(page)
    page.goto("/login")
    with page.expect_event("pageerror"):
        page.evaluate("setTimeout(() => { throw new Error('kaboom') }, 0)")
    assert any("kaboom" in error for error in errors), errors


def test_require_browser_skips_locally_but_fails_under_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from playwright.sync_api import Error

    class Broken:
        def launch(self) -> None:
            raise Error("Executable doesn't exist at /nowhere\nmore detail")

    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception, match="make e2e"):
        testing.require_browser(Broken())  # type: ignore[arg-type]

    monkeypatch.setenv("CI", "false")
    with pytest.raises(pytest.skip.Exception, match="make e2e"):
        testing.require_browser(Broken())  # type: ignore[arg-type]

    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match=r"Executable doesn't exist at /nowhere$"):
        testing.require_browser(Broken())  # type: ignore[arg-type]


def test_the_reason_given_is_the_missing_library_not_the_first_line() -> None:
    error = (
        "BrowserType.launch: Target page, context or browser has been closed\n"
        "  - [pid=1][err] /x/chrome: error while loading shared libraries: libnspr4.so: "
        "cannot open shared object file\n"
    )
    assert testing._cause(error).startswith("/x/chrome: error while loading shared libraries")
    assert testing._cause("just one line") == "just one line"
    assert testing._cause("") == "unknown error"
