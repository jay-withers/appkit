"""Browser-test helpers: run the real app, sign in, and catch console errors.

For each app's `tests/e2e/`, with pytest-playwright (the `e2e` extra). The
unit suites test routes through `TestClient`; these drive Chromium against the
app served by uvicorn, so they catch what only a browser sees: a template that
renders but breaks in the page, a script the CSP blocks for a missing nonce, a
cookie a browser refuses.

pytest reads `pytest_plugins` only from the top-level conftest, so this is
plain functions rather than a plugin. An app's `tests/e2e/conftest.py` wires
them into fixtures, overriding pytest-playwright's own:

    @pytest.fixture(scope="session")
    def browser_type(browser_type):  # checked before anything launches
        testing.require_browser(browser_type)
        return browser_type

    @pytest.fixture(scope="session")
    def base_url():
        argv = [testing.console_script("gymlog"), "serve", "--port", "{port}"]
        with testing.live_server(argv, env={"KEY_VAULT_URI": None, ...}) as url:
            yield url
"""

from __future__ import annotations

import contextlib
import http.client
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import BrowserType, ConsoleMessage, Page

# Bound to the loopback address and reached by IP rather than `localhost`,
# which can resolve to ::1 first while the server listens on IPv4 only.
# Chromium treats 127.0.0.1 as a secure context, so the apps' `Secure` session
# cookie is kept over plain http here, as it is over https in Azure.
HOST = "127.0.0.1"


def free_port() -> int:
    """A TCP port nothing is listening on at the moment of asking."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        port: int = sock.getsockname()[1]
        return port


def console_script(name: str) -> str:
    """The path of a console script installed in the running interpreter's venv.

    Not `uv run <name>`: the test is already inside the venv, and a second uv
    resolution could pick a different environment.
    """
    return str(Path(sys.executable).with_name(name))


@contextmanager
def live_server(
    argv: Sequence[str],
    *,
    env: Mapping[str, str | None] | None = None,
    health_path: str = "/healthz",
    timeout: float = 30.0,
) -> Iterator[str]:
    """Run the app in a subprocess on a free port and yield its base URL.

    `{port}` and `{host}` in `argv` are filled in. `env` is laid over the
    current environment, and a `None` value removes that variable: a session
    server starts before any function-scoped fixture clears the environment,
    so it must be told what to drop. Ready means `health_path` answers 200; if
    the process exits or `timeout` passes first, this raises with the server's
    output, so a crash at import reads as the crash rather than as a refused
    connection.

    The server runs in a session of its own, and the whole process group is
    stopped (SIGTERM, then SIGKILL) when the block exits, so a reloader's or a
    wrapper's child can't outlive it holding the port.
    """
    port = free_port()
    command = [part.format(port=port, host=HOST) for part in argv]
    base_url = f"http://{HOST}:{port}"
    merged = dict(os.environ)
    for name, value in (env or {}).items():
        if value is None:
            merged.pop(name, None)
        else:
            merged[name] = value
    # Output goes to a file, so Python would block-buffer it, and a server that
    # is up but unhealthy would show no output in the timeout error.
    merged.setdefault("PYTHONUNBUFFERED", "1")

    with tempfile.TemporaryFile(mode="w+") as output:
        # S603: the command is the test's own, never user input.
        process = subprocess.Popen(  # noqa: S603
            command,
            env=merged,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            _wait_until_healthy(process, base_url + health_path, timeout, output)
            yield base_url
        finally:
            _stop(process)


def _stop(process: subprocess.Popen[str]) -> None:
    """SIGTERM the server's process group, then SIGKILL whatever is left of it.

    The SIGKILL goes to the group even when the server exited cleanly, to sweep
    a child it left behind; a group with nobody left in it is not an error.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=10)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


# Never through a proxy: urllib honours http_proxy, and a loopback probe sent
# to one fails although the app is up.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _wait_until_healthy(
    process: subprocess.Popen[str], url: str, timeout: float, output: IO[str]
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"the server exited with {process.returncode} before {url} answered:\n"
                + _tail(output)
            )
        try:
            # S310: a loopback http URL this module built.
            with _DIRECT.open(url, timeout=2) as response:
                if response.status == 200:
                    return
        except urllib.error.HTTPError as exc:
            exc.close()  # an HTTP error holds the open response
        except (urllib.error.URLError, http.client.HTTPException, OSError):
            pass  # not listening yet, or a half-started server dropped the request
        time.sleep(0.1)
    raise RuntimeError(f"{url} did not answer 200 within {timeout:g}s:\n" + _tail(output))


def _tail(output: IO[str], lines: int = 40) -> str:
    output.flush()
    output.seek(0)
    return "".join(output.readlines()[-lines:])


def require_browser(browser_type: BrowserType) -> None:
    """Skip when Chromium can't start here, but fail under CI.

    A developer without `playwright install chromium` gets a skip that says how
    to get it. Under CI (the `CI` variable GitHub Actions sets) the browser is
    installed by the workflow, so a browser that can't start is a broken
    install, and passing it as skipped would hide every browser test.
    """
    import pytest
    from playwright.sync_api import Error

    try:
        browser_type.launch().close()
    except Error as exc:
        message = f"Chromium can't start: {_cause(str(exc))}"
        if os.environ.get("CI", "").lower() not in ("", "0", "false"):
            pytest.fail(message, pytrace=False)
        pytest.skip(f"{message}. Run `make e2e`, which installs it.")


def _cause(error: str) -> str:
    """The line of a launch error that says why: a missing library or binary."""
    lines = error.splitlines()
    for line in lines:
        if "error while loading shared libraries" in line or "Executable doesn't exist" in line:
            return line.split("] ", 1)[-1].strip()
    return lines[0] if lines else "unknown error"


def watch_console(page: Page) -> list[str]:
    """Collect the page's console errors and uncaught exceptions into a list.

    A CSP violation is a console error ("Refused to execute inline script..."),
    so asserting the list is empty after a journey catches a script or style
    that lost its nonce, which no TestClient test can see.
    """
    errors: list[str] = []

    def on_console(message: ConsoleMessage) -> None:
        if message.type == "error":
            errors.append(message.text)

    page.on("console", on_console)
    page.on("pageerror", lambda error: errors.append(str(error)))
    return errors


def log_in(page: Page, passcode: str, *, login_path: str = "/login") -> None:
    """Sign in through the app's own login form (appkit.passcode's gate).

    `login_path` is the gate's. Relative to the page context's base URL. Waits
    until the browser has left the login page, so a rejected passcode fails
    here, at the step that broke.
    """
    page.goto(login_path)
    page.fill('input[name="passcode"]', passcode)
    page.click(f'form[action="{login_path}"] [type="submit"]')
    page.wait_for_url(lambda url: not url.split("?", 1)[0].endswith(login_path))
