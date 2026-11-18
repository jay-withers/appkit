"""The nightly encrypted copy to R2 (backup.py), and its proof that it restores.

R2 is an in-memory fake with the four S3 calls the job makes. The age keys are
real, so a test that passes here has actually encrypted and decrypted. The
documents are a dict standing in for an app's store.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import pyrage
import pytest
from pyrage import x25519

from appkit.backup import DRILL_KEY, Backup, Document

OWNER = x25519.Identity.generate()
DRILL = x25519.Identity.generate()
BUCKET = "test-backups"

MID_MONTH = datetime(2026, 11, 15, 2, 30, 5, tzinfo=UTC)
DAILY = "daily/an-app/2026-11-15T023005Z/doc.json.age"
MONTHLY = "monthly/an-app/2026-11/doc.json.age"


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeR2:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []
        self.listed: list[dict[str, Any]] = []
        # Armed by a test: rewrite what comes back, as a corrupted store would.
        self.tamper = False
        # Armed by a test: refuse uploads whose key contains this.
        self.refuse = ""

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        assert Bucket == BUCKET
        if self.refuse and self.refuse in Key:
            raise ConnectionError("R2 is having a day")
        self.puts.append(Key)
        self.objects[Key] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        assert Bucket == BUCKET
        data = self.objects[Key]
        if self.tamper:
            data = pyrage.encrypt(b"something else", [DRILL.to_public()])
        return {"Body": _Body(data)}

    def list_objects_v2(self, *, Bucket: str, Prefix: str, MaxKeys: int) -> dict[str, Any]:
        assert Bucket == BUCKET
        self.listed.append({"Bucket": Bucket, "Prefix": Prefix, "MaxKeys": MaxKeys})
        keys = sorted(k for k in self.objects if k.startswith(Prefix))[:MaxKeys]
        return {"Contents": [{"Key": k} for k in keys]} if keys else {}


@dataclass(frozen=True)
class FakeSettings:
    backup_bucket: str = BUCKET
    backup_r2_endpoint: str = "https://acc.eu.r2.cloudflarestorage.com"
    backup_age_recipient: str = str(OWNER.to_public())


def _parse(raw: bytes) -> None:
    if not isinstance(json.loads(raw), dict):
        raise ValueError("not the stored shape")


class World:
    """The app around a Backup: its settings, secrets and stored documents."""

    def __init__(self) -> None:
        self.settings = FakeSettings()
        # As Key Vault returns it when set from a file: with a trailing newline.
        self.secrets = {
            DRILL_KEY: f"{DRILL}\n",
            "R2-ACCESS-KEY-ID": "the-id",
            "R2-SECRET-ACCESS-KEY": "the-secret",
        }
        self.stored: dict[str, bytes] = {}

    def secret(self, name: str) -> str:
        if name not in self.secrets:
            raise RuntimeError(f"{name} is not set")
        return self.secrets[name]

    def loader(self, name: str):
        return lambda: self.stored.get(name)


@pytest.fixture
def world() -> World:
    w = World()
    w.stored["doc.json"] = b'{"a": 1}'
    return w


@pytest.fixture
def job(world: World) -> Backup:
    return Backup(
        "an-app",
        [
            Document("doc.json", world.loader("doc.json"), _parse, required=True),
            Document("extra.json", world.loader("extra.json"), _parse, required=False),
        ],
        settings=lambda: world.settings,
        secret=world.secret,
    )


@pytest.fixture
def r2() -> FakeR2:
    return FakeR2()


def test_copies_the_document_encrypted_and_the_owner_can_decrypt_it(job, r2, world):
    assert job.run(client=r2, now=MID_MONTH) == 0
    sealed = r2.objects[DAILY]
    assert world.stored["doc.json"] not in sealed
    assert pyrage.decrypt(sealed, [OWNER]) == world.stored["doc.json"]


def test_the_drill_key_decrypts_it_too(job, r2, world):
    job.run(client=r2, now=MID_MONTH)
    assert pyrage.decrypt(r2.objects[DAILY], [DRILL]) == world.stored["doc.json"]


def test_the_first_run_in_a_month_also_keeps_a_monthly_copy(job, r2):
    assert job.run(client=r2, now=MID_MONTH) == 0
    assert set(r2.objects) == {DAILY, MONTHLY}


def test_later_runs_that_month_keep_only_the_daily(job, r2):
    job.run(client=r2, now=MID_MONTH)
    job.run(client=r2, now=datetime(2026, 11, 16, 2, 30, tzinfo=UTC))
    assert r2.puts.count(MONTHLY) == 1
    assert "daily/an-app/2026-11-16T023000Z/doc.json.age" in r2.objects


def test_a_month_whose_first_run_failed_still_gets_its_monthly_copy(job, r2):
    r2.refuse = "an-app"
    assert job.run(client=r2, now=datetime(2026, 12, 1, 2, 30, tzinfo=UTC)) == 1
    r2.refuse = ""
    assert job.run(client=r2, now=datetime(2026, 12, 2, 2, 30, tzinfo=UTC)) == 0
    assert "monthly/an-app/2026-12/doc.json.age" in r2.objects


def test_a_rerun_adds_a_copy_and_never_replaces_one(job, r2, world):
    job.run(client=r2, now=MID_MONTH)
    first = r2.objects[DAILY]
    world.stored["doc.json"] = b"{gone bad"
    job.run(client=r2, now=datetime(2026, 11, 15, 14, 0, tzinfo=UTC))
    assert r2.objects[DAILY] == first
    assert r2.objects[MONTHLY] == first
    assert len(r2.puts) == len(set(r2.puts))


def test_an_optional_document_is_copied_when_there_is_one(job, r2, world):
    world.stored["extra.json"] = b"{}"
    assert job.run(client=r2, now=MID_MONTH) == 0
    assert "daily/an-app/2026-11-15T023005Z/extra.json.age" in r2.objects


def test_an_optional_document_the_app_could_not_read_fails(job, r2, world, caplog):
    world.stored["extra.json"] = b'["not", "the", "stored", "shape"]'
    with caplog.at_level(logging.INFO):
        assert job.run(client=r2, now=MID_MONTH) == 1
    assert "FAIL extra.json" in caplog.text


def test_an_optional_document_never_written_is_skipped_not_failed(job, r2, caplog):
    with caplog.at_level(logging.INFO):
        assert job.run(client=r2, now=MID_MONTH) == 0
    assert "skip extra.json" in caplog.text


def test_a_missing_required_document_fails(job, r2, world, caplog):
    del world.stored["doc.json"]
    with caplog.at_level(logging.INFO):
        assert job.run(client=r2, now=MID_MONTH) == 1
    assert "FAIL doc.json: the document does not exist" in caplog.text
    assert r2.objects == {}


def test_a_copy_that_comes_back_different_fails(job, r2, caplog):
    r2.tamper = True
    with caplog.at_level(logging.INFO):
        assert job.run(client=r2, now=MID_MONTH) == 1
    assert "differs" in caplog.text


def test_a_bad_copy_never_takes_the_months_slot(job, r2, world):
    """The monthly copy comes only from a daily copy that restored."""
    good = world.stored["doc.json"]
    world.stored["doc.json"] = b"{not json"
    assert job.run(client=r2, now=datetime(2026, 12, 1, 2, 30, tzinfo=UTC)) == 1
    assert "monthly/an-app/2026-12/doc.json.age" not in r2.objects

    world.stored["doc.json"] = good
    assert job.run(client=r2, now=datetime(2026, 12, 2, 2, 30, tzinfo=UTC)) == 0
    monthly = r2.objects["monthly/an-app/2026-12/doc.json.age"]
    assert pyrage.decrypt(monthly, [OWNER]) == good


def test_a_failed_monthly_lookup_still_keeps_the_daily(job, r2, monkeypatch, caplog):
    def broken(**_: object) -> None:
        raise ConnectionError("R2 is having a day")

    monkeypatch.setattr(r2, "list_objects_v2", broken)
    with caplog.at_level(logging.INFO):
        assert job.run(client=r2, now=MID_MONTH) == 1
    assert DAILY in r2.objects
    assert f"FAIL doc.json -> {MONTHLY}: ConnectionError" in caplog.text


def test_a_corrupt_document_is_still_copied_then_fails(job, r2, world):
    world.stored["doc.json"] = b"{not json"
    assert job.run(client=r2, now=MID_MONTH) == 1
    assert pyrage.decrypt(r2.objects[DAILY], [OWNER]) == b"{not json"


def test_a_failed_upload_is_counted_and_the_rest_still_go(job, r2, world, caplog):
    world.stored["extra.json"] = b"{}"
    r2.refuse = "doc.json"
    with caplog.at_level(logging.INFO):
        assert job.run(client=r2, now=MID_MONTH) == 1
    assert "FAIL doc.json" in caplog.text
    assert "daily/an-app/2026-11-15T023005Z/extra.json.age" in r2.objects


def test_an_unreadable_document_is_counted_and_the_rest_still_go(world, r2, caplog):
    def down() -> bytes:
        raise RuntimeError("blob down")

    world.stored["extra.json"] = b"{}"
    job = Backup(
        "an-app",
        [
            Document("doc.json", down, _parse, required=True),
            Document("extra.json", world.loader("extra.json"), _parse, required=False),
        ],
        settings=lambda: world.settings,
        secret=world.secret,
    )
    with caplog.at_level(logging.INFO):
        assert job.run(client=r2, now=MID_MONTH) == 1
    assert "FAIL doc.json: RuntimeError: blob down" in caplog.text
    assert "daily/an-app/2026-11-15T023005Z/extra.json.age" in r2.objects


def test_keys_rejects_a_malformed_drill_key_without_echoing_it(job, world):
    world.secrets[DRILL_KEY] = "AGE-SECRET-KEY-NOTAKEY"
    with pytest.raises(ValueError, match="BACKUP-AGE-DRILL-KEY is not") as raised:
        job.keys()
    assert "NOTAKEY" not in str(raised.value)


def test_keys_rejects_a_malformed_recipient(job, world):
    world.settings = replace(world.settings, backup_age_recipient="age1nope")
    with pytest.raises(ValueError, match="BACKUP_AGE_RECIPIENT is not"):
        job.keys()


@pytest.mark.parametrize(
    ("field", "unset"),
    [
        ("backup_bucket", "BACKUP_BUCKET"),
        ("backup_r2_endpoint", "BACKUP_R2_ENDPOINT"),
        ("backup_age_recipient", "BACKUP_AGE_RECIPIENT"),
    ],
)
def test_refuses_to_run_half_configured(job, r2, world, field, unset, caplog):
    world.settings = replace(world.settings, **{field: ""})
    with caplog.at_level(logging.INFO):
        assert job.run(client=r2, now=MID_MONTH) == 1
    assert unset in caplog.text
    assert r2.objects == {}


def test_never_logs_a_key(job, r2, caplog):
    with caplog.at_level(logging.DEBUG):
        job.run(client=r2, now=MID_MONTH)
    assert "AGE-SECRET-KEY" not in caplog.text


def test_a_key_vault_error_is_reported_as_itself_not_as_a_bad_key(job, r2, world, caplog):
    """A drill key Key Vault can't hand over is not a malformed one (from career #148)."""
    del world.secrets[DRILL_KEY]
    with caplog.at_level(logging.INFO):
        assert job.run(client=r2, now=MID_MONTH) == 1
    assert "FAIL setup: RuntimeError: BACKUP-AGE-DRILL-KEY is not set" in caplog.text
    assert "not an age secret key" not in caplog.text
    assert r2.objects == {}


def test_an_r2_client_that_cannot_be_built_is_logged_not_raised(job, world, caplog):
    del world.secrets["R2-ACCESS-KEY-ID"]
    with caplog.at_level(logging.INFO):
        assert job.run(now=MID_MONTH) == 1
    assert "FAIL setup: RuntimeError: R2-ACCESS-KEY-ID is not set" in caplog.text


def test_check_bucket_lists_this_apps_prefix_and_writes_nothing(job, r2):
    job.check_bucket(r2)
    assert r2.listed == [{"Bucket": BUCKET, "Prefix": "daily/an-app/", "MaxKeys": 1}]
    assert r2.objects == {}


def test_r2_client_points_at_the_endpoint_with_the_vault_keys(job):
    client = job.r2_client()
    assert client.meta.endpoint_url == "https://acc.eu.r2.cloudflarestorage.com"
    assert client.meta.region_name == "auto"


def test_only_the_job_given_a_bucket_is_configured(job, world):
    assert job.configured()
    world.settings = replace(world.settings, backup_bucket="")
    assert not job.configured()


def test_settings_that_do_not_load_read_as_not_configured(world):
    def broken() -> FakeSettings:
        raise ValueError("bad settings")

    assert not Backup("an-app", [], settings=broken, secret=world.secret).configured()


def test_two_documents_of_one_name_are_refused(world):
    """They would share a key, and the second would replace the first's copy."""
    doc = Document("doc.json", world.loader("doc.json"), _parse, required=True)
    with pytest.raises(ValueError, match="unique"):
        Backup("an-app", [doc, doc], settings=lambda: world.settings, secret=world.secret)
