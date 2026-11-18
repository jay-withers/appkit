"""A minimal app wired like the real ones, for test_testing.py's browser tests.

Served by uvicorn in a subprocess (appkit.testing.live_server), so it is a
module of its own rather than a fixture: headers.install for the CSP,
PasscodeGate for the session cookie, and a login form shaped like the apps'.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from appkit import headers
from appkit.passcode import PasscodeGate

PASSCODE = "open-sesame"
SETTINGS = SimpleNamespace(require_passcode=True, cookie_secret="", cookie_max_age_seconds=3600)

gate = PasscodeGate("e2e_session", settings=lambda: SETTINGS, secret=lambda _name: PASSCODE)
app = FastAPI()
headers.install(app)

LOGIN = """<!doctype html><title>login</title>
<form method="post" action="/login"><p>{error}</p>
<input type="password" name="passcode"><button type="submit">In</button></form>"""


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_form() -> str:
    return LOGIN.format(error="")


@app.post("/login")
def login(passcode: Annotated[str, Form()] = "") -> Any:
    if not gate.check_passcode(passcode):
        return HTMLResponse(LOGIN.format(error="no"), status_code=status.HTTP_401_UNAUTHORIZED)
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(gate.cookie_name, gate.issue(), httponly=True, samesite="lax", secure=True)
    return response


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(gate.require_session)])
def home(request: Request) -> str:
    nonce = request.state.csp_nonce
    return f'<!doctype html><title>home</title><h1>in</h1><script nonce="{nonce}">1</script>'


@app.get("/no-nonce", response_class=HTMLResponse)
def no_nonce() -> str:
    """An inline script without the nonce: the CSP must block it."""
    return "<!doctype html><title>bad</title><script>document.title = 'ran'</script>"
