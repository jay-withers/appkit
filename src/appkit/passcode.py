"""The shared-passcode gate in front of every app's pages.

Was each app's `api/deps.py`, copied from gym-log with only the cookie name
changed. An app builds one `PasscodeGate` and routes through it.

Each app is reachable from the public internet the moment ingress is external,
so the gate defaults **on** (an app's `require_passcode` setting) and a missing
passcode fails closed rather than open.

It is a shared passcode rather than Entra sign-in, a considered trade:
Container Apps' built-in auth is not exposed by `azurerm` and would need the
`azapi` provider, which no repo in this estate uses, in exchange for an Entra
round trip in front of a training log or a household's accounts.

The session cookie is signed rather than encrypted. It carries no secret, only
an issue time, and the signature is what makes it unforgeable. Its key is
derived from the passcode when `cookie_secret` is unset, which has the useful
property that rotating the passcode invalidates every outstanding session.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import time
from collections.abc import Callable
from typing import Annotated, Protocol

from fastapi import Cookie, HTTPException, status

# The Key Vault secret holding the passcode, in every app.
PASSCODE_SECRET = "APP-PASSCODE"  # noqa: S105 - a name, not a value

# How far ahead of this replica's clock a token may be stamped and still count.
CLOCK_SKEW_SECONDS = 300


class PasscodeSettings(Protocol):
    """The fields of an app's `Settings` the gate reads."""

    @property
    def require_passcode(self) -> bool: ...

    @property
    def cookie_secret(self) -> str: ...

    @property
    def cookie_max_age_seconds(self) -> int: ...


class PasscodeGate:
    """Issue and check session cookies, and gate pages behind them.

    `settings` and `secret` are the app's own (cached) functions, called on
    every use rather than captured once, so a test that changes the
    environment and clears the app's caches sees the change here too.
    """

    def __init__(
        self,
        cookie_name: str,
        *,
        settings: Callable[[], PasscodeSettings],
        secret: Callable[[str], str],
        login_path: str = "/login",
    ) -> None:
        self.cookie_name = cookie_name
        self.login_path = login_path
        self._settings = settings
        self._secret = secret
        self.require_session = self._dependency()

    def signing_key(self) -> bytes:
        """The key the session cookie is signed with.

        Derived from the passcode rather than generated when `cookie_secret` is
        unset: a generated key would live in process memory, so every deploy,
        and every scale-from-zero at `min_replicas = 0`, would silently log
        every browser out again.
        """
        configured = self._settings().cookie_secret
        if configured:
            return configured.encode("utf-8")
        passcode = self._secret(PASSCODE_SECRET)
        if not passcode:
            # sha256 of nothing is a key anyone can compute, so every forged
            # cookie would pass. Fail closed: no session is valid or issued.
            raise ValueError(f"{PASSCODE_SECRET} is empty and no cookie secret is set")
        return hashlib.sha256(passcode.encode("utf-8")).digest()

    def issue(self) -> str:
        """A signed session token for a browser that has presented the passcode."""
        issued = str(int(time.time()))
        signature = hmac.new(self.signing_key(), issued.encode("ascii"), hashlib.sha256)
        return f"{issued}.{signature.hexdigest()}"

    def valid(self, token: str | None) -> bool:
        """Whether `token` is one this gate issued and has not expired."""
        if not token or "." not in token:
            return False
        issued, _, signature = token.partition(".")
        # isascii too: str.isdigit accepts digits from other scripts (Arabic-Indic),
        # which int() parses but .encode("ascii") below would raise on.
        if not (issued.isascii() and issued.isdigit()):
            return False

        try:
            key = self.signing_key()
        except ValueError:
            return False
        expected = hmac.new(key, issued.encode("ascii"), hashlib.sha256).hexdigest()
        # Constant-time: a plain != leaks the matching prefix length to anyone
        # willing to time the responses. Bytes, because compare_digest raises
        # TypeError on a str holding anything outside ASCII, and the cookie is
        # whatever the client sent.
        if not hmac.compare_digest(signature.encode("utf-8"), expected.encode("ascii")):
            return False

        # A token from the future (a replica's clock running fast) would
        # otherwise outlive its lifetime by however far ahead it was stamped.
        age = time.time() - int(issued)
        return -CLOCK_SKEW_SECONDS <= age < self._settings().cookie_max_age_seconds

    def check_passcode(self, presented: str) -> bool:
        """Whether `presented` is the configured passcode.

        Compared as bytes rather than as `str`: `hmac.compare_digest` raises
        `TypeError` if *either* string holds a character outside ASCII, so a
        passcode containing a `£` turned every login into a 500 rather than a
        refusal. Encoding both sides first is also what makes the comparison
        well-defined: two strings are equal here only if their UTF-8 is.
        """
        expected = self._secret(PASSCODE_SECRET)
        return bool(expected) and hmac.compare_digest(
            presented.encode("utf-8"), expected.encode("utf-8")
        )

    def _dependency(self) -> Callable[..., None]:
        gate = self

        def require_session(session: str | None = None) -> None:
            """Gate a page behind the passcode, redirecting to the login form if not met.

            A 303 rather than a 401: every consumer of this is an HTML page
            opened directly in a browser, and a browser shown a 401 renders the
            error rather than the login form.
            """
            if not gate._settings().require_passcode:
                return
            if gate.valid(session):
                return
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                detail="passcode required",
                headers={"Location": gate.login_path},
            )

        # The cookie's name is only known here, so the parameter FastAPI reads
        # is declared as an object, not an annotation: with postponed
        # annotations, a string naming `gate` can't be resolved from this
        # module's globals, and FastAPI would read `session` as a query
        # parameter instead.
        require_session.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            [
                inspect.Parameter(
                    "session",
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=None,
                    annotation=Annotated[str | None, Cookie(alias=gate.cookie_name)],
                )
            ],
            return_annotation=None,
        )
        return require_session
