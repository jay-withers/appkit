"""The checks behind every app's `selfcheck` command, and the loop that runs them.

After every deploy, `make smoke` starts one run of each job with its args
swapped for `selfcheck`. That run gets everything a real one does (the image,
the managed identity, the env and the Key Vault references) and checks what a
real run needs before it does anything. It sends no email, calls no model,
logs in to nothing and writes nothing.

An app lists its own checks (settings, its secrets, its storage, and its
documents' integrity through `integrity_checks`) and passes
them to `run()`, which exits 1 if any fails, naming each failure but never a
secret's value. A failure fails the smoke test, which rolls the deploy back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from functools import partial
from typing import Any

from . import integrity
from .backup import SECRETS as BACKUP_SECRETS
from .backup import Backup

logger = logging.getLogger(__name__)

# A label for the log line, and something to call that raises on failure.
Check = tuple[str, Callable[[], object]]


def _present(secret: Callable[[str], str], name: str) -> None:
    if not secret(name):
        raise ValueError("resolved to an empty value")


def secret_checks(names: Iterable[str], secret: Callable[[str], str]) -> list[Check]:
    """One check per required secret: it resolves, and not to an empty value."""
    return [(f"secret {name}", partial(_present, secret, name)) for name in names]


def optional_secret_checks(
    names: Iterable[str], optional_secret: Callable[[str], str | None]
) -> list[Check]:
    """One check per optional secret: resolving it doesn't raise (absent is fine,
    a broken credential is not)."""
    return [(f"secret {name} (optional)", partial(optional_secret, name)) for name in names]


def backup_checks(backup: Backup) -> list[Check]:
    """The backup job's checks, or none in any other job.

    Only the backup job is given a bucket, so a missing R2 key fails that job's
    selfcheck without reading as a fault in the others.
    """
    if not backup.configured():
        return []
    return [
        # The backup's own resolver, so the check reads what the job will.
        *secret_checks(BACKUP_SECRETS, backup.secret),
        ("backup settings and keys", backup.keys),
        ("backup bucket", backup.check_bucket),
    ]


def integrity_checks(
    name: str,
    load: Callable[[], bytes | None],
    parse: Callable[[bytes], Any],
    dump: Callable[[Any], str | bytes],
    check: Callable[[Any], list[str]] | None = None,
) -> list[Check]:
    """One check that the stored document `name` survives this code's rewrite and
    passes the app's own `check` (see `appkit.integrity`).

    `load` returns the bytes exactly as stored, or None for a document never
    written, which passes: a first run has nothing to lose.
    """

    def verify() -> None:
        raw = load()
        if raw is not None:
            integrity.require(raw, parse, dump, check)

    return [(f"integrity {name}", verify)]


def run(checks: Iterable[Check]) -> int:
    """Run every check, logging each. 0 if all pass, else 1."""
    failed = 0
    for label, check in checks:
        try:
            check()
        except Exception as exc:
            failed += 1
            logger.error("selfcheck: FAIL %s: %s: %s", label, type(exc).__name__, str(exc)[:300])
        else:
            logger.info("selfcheck: ok %s", label)
    if failed:
        logger.error("selfcheck: %d check(s) failed", failed)
        return 1
    logger.info("selfcheck: all checks passed")
    return 0
