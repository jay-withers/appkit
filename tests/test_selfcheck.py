"""selfcheck.py: the check loop and the shared check builders."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from appkit import selfcheck
from appkit.backup import SECRETS as BACKUP_SECRETS
from appkit.backup import Backup


def _secrets(values: dict[str, str]):
    def secret(name: str) -> str:
        if name not in values:
            raise RuntimeError(f"{name} is not set")
        return values[name]

    return secret


def test_all_passing_is_0(caplog):
    with caplog.at_level(logging.INFO):
        assert selfcheck.run([("one", lambda: None), ("two", lambda: 1)]) == 0
    assert "selfcheck: ok one" in caplog.text
    assert "selfcheck: all checks passed" in caplog.text


def test_every_check_runs_and_each_failure_is_named(caplog):
    ran: list[str] = []

    def fail() -> None:
        ran.append("fail")
        raise ConnectionError("unreachable")

    with caplog.at_level(logging.INFO):
        assert selfcheck.run([("first", fail), ("second", lambda: ran.append("second"))]) == 1
    assert ran == ["fail", "second"]
    assert "selfcheck: FAIL first: ConnectionError: unreachable" in caplog.text
    assert "selfcheck: 1 check(s) failed" in caplog.text


def test_a_secret_check_fails_on_missing_or_empty_and_never_logs_a_value(caplog):
    secret = _secrets({"SET": "the-value", "EMPTY": ""})
    checks = selfcheck.secret_checks(["SET", "EMPTY", "MISSING"], secret)
    assert [label for label, _ in checks] == ["secret SET", "secret EMPTY", "secret MISSING"]
    with caplog.at_level(logging.INFO):
        assert selfcheck.run(checks) == 1
    assert "FAIL secret EMPTY: ValueError: resolved to an empty value" in caplog.text
    assert "FAIL secret MISSING: RuntimeError" in caplog.text
    assert "the-value" not in caplog.text


def test_an_absent_optional_secret_passes_but_a_broken_one_fails():
    def optional(name: str) -> str | None:
        if name == "BROKEN":
            raise PermissionError("credential broken")
        return None

    checks = selfcheck.optional_secret_checks(["ABSENT", "BROKEN"], optional)
    assert checks[0][0] == "secret ABSENT (optional)"
    assert selfcheck.run(checks[:1]) == 0
    assert selfcheck.run(checks[1:]) == 1


@dataclass(frozen=True)
class FakeSettings:
    backup_bucket: str = ""
    backup_r2_endpoint: str = ""
    backup_age_recipient: str = ""


def test_a_job_with_no_bucket_gets_no_backup_checks():
    backup = Backup("an-app", [], settings=FakeSettings, secret=_secrets({}))
    assert selfcheck.backup_checks(backup) == []


def test_the_backup_job_checks_its_secrets_keys_and_bucket():
    backup = Backup(
        "an-app", [], settings=lambda: FakeSettings(backup_bucket="b"), secret=_secrets({})
    )
    labels = [label for label, _ in selfcheck.backup_checks(backup)]
    assert labels == [
        *(f"secret {name}" for name in BACKUP_SECRETS),
        "backup settings and keys",
        "backup bucket",
    ]


def test_the_backup_checks_report_a_half_configured_job(caplog):
    backup = Backup(
        "an-app", [], settings=lambda: FakeSettings(backup_bucket="b"), secret=_secrets({})
    )
    with caplog.at_level(logging.INFO):
        assert selfcheck.run(selfcheck.backup_checks(backup)) == 1
    assert "FAIL backup settings and keys: ValueError" in caplog.text
