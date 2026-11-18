"""The nightly off-Azure backup: copy each document, encrypted, and prove it restores.

Was each app's `backup.py` (gym-log's, adapted by finances and career). An app
declares its documents and builds one `Backup`; its `backup` CLI command calls
`run()` and its selfcheck adds `selfcheck.backup_checks(...)`.

An app's documents live in one LRS storage account in one subscription.
Versioning, soft delete and the delete lock cover a bad write or a slip of the
hand; they do nothing if the subscription itself is disabled or lost. So once
a day the app's backup job reads each document *exactly as stored*, encrypts
it with age, and puts it in Cloudflare R2 (the `cloudflare` repo's
`jw-app-backups` bucket) under `daily/<app>/<UTC timestamp>/`. The first run in
a month whose daily copy verifies, and that finds no `monthly/<app>/<yyyy-mm>/`
copy, writes one there too: a run that fails on the 1st doesn't leave the month
without one, and a bad copy never takes the month's slot. R2's lifecycle rules
are the only retention; nothing here deletes.

**Nothing is ever overwritten.** Every key carries the moment it was taken (and
a monthly copy is only written where none exists), so a rerun or a retry, even
one reading a document that went bad since, adds a copy instead of replacing a
good one.

**Two age recipients.** Every copy is encrypted to the owner's public key
(`BACKUP_AGE_RECIPIENT`), whose private half stays offline, and to a separate
*drill* key whose private half is the Key Vault secret `BACKUP-AGE-DRILL-KEY`.
Either can decrypt. The drill key is what lets the job prove, on every run,
that what it uploaded comes back: it downloads each object again, decrypts it,
checks it is byte-for-byte what it read, and parses it the way the app does. A
backup that has never been restored is a hope, not a backup. Whoever can read
the drill key can already read the blob itself, so it widens nothing.

**Copy first, judge second.** A document that fails to parse is still uploaded
and then fails the run, so the job-failure alert fires while the last good
copies are still in R2. A failure with one document or key is logged and
counted, and the rest are still attempted.

Needs the `backup` extra (pyrage, boto3), imported only inside the functions
that use them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# The Key Vault secrets the backup job reads, by name. selfcheck checks each.
R2_ACCESS_KEY_ID = "R2-ACCESS-KEY-ID"
R2_SECRET_ACCESS_KEY = "R2-SECRET-ACCESS-KEY"  # noqa: S105 - a name, not a value
DRILL_KEY = "BACKUP-AGE-DRILL-KEY"
SECRETS = (R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, DRILL_KEY)


class BackupSettings(Protocol):
    """The fields of an app's `Settings` the backup reads. Only the backup job
    is given them; an empty bucket means "not the backup job"."""

    @property
    def backup_bucket(self) -> str: ...

    @property
    def backup_r2_endpoint(self) -> str: ...

    @property
    def backup_age_recipient(self) -> str: ...


@dataclass(frozen=True)
class Document:
    """One document an app backs up."""

    # The name it is stored under, and the last part of its R2 key.
    name: str
    # The bytes exactly as stored, or None if it has never been written.
    load: Callable[[], bytes | None]
    # Parses the bytes or raises: what "restores" means for this document.
    validate: Callable[[bytes], object]
    # A missing optional document is skipped; a missing required one fails.
    required: bool


class Backup:
    """One app's backup: its documents, its settings and its secrets."""

    def __init__(
        self,
        app: str,
        documents: Sequence[Document],
        *,
        settings: Callable[[], BackupSettings],
        secret: Callable[[str], str],
    ) -> None:
        # The key prefix under daily/ and monthly/, shared by every app writing
        # to the bucket. The repository's name, so a restore knows where to look.
        self.app = app
        self.documents = tuple(documents)
        # Two documents of one name would share a key, and the second would
        # replace the first's verified copy within the same run.
        names = [doc.name for doc in self.documents]
        if len(names) != len(set(names)):
            raise ValueError(f"document names must be unique, got {names}")
        self._settings = settings
        self.secret = secret

    def daily_key(self, name: str, now: datetime) -> str:
        return f"daily/{self.app}/{now:%Y-%m-%dT%H%M%SZ}/{name}.age"

    def monthly_key(self, name: str, now: datetime) -> str:
        return f"monthly/{self.app}/{now:%Y-%m}/{name}.age"

    def configured(self) -> bool:
        """Whether this process is the backup job (it alone is given a bucket).

        Settings that don't load are someone else's failure to report, so they
        read as "not the backup job" rather than raising.
        """
        try:
            return bool(self._settings().backup_bucket)
        except Exception:
            return False

    def r2_client(self) -> Any:
        """An S3 client for the R2 bucket, from the settings and Key Vault."""
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=self._settings().backup_r2_endpoint,
            aws_access_key_id=self.secret(R2_ACCESS_KEY_ID),
            aws_secret_access_key=self.secret(R2_SECRET_ACCESS_KEY),
            region_name="auto",
            config=Config(retries={"max_attempts": 5, "mode": "standard"}),
        )

    def check_bucket(self, client: Any | None = None) -> None:
        """Prove the R2 keys can list this app's prefix, writing nothing (selfcheck)."""
        client = client or self.r2_client()
        client.list_objects_v2(
            Bucket=self._settings().backup_bucket, Prefix=f"daily/{self.app}/", MaxKeys=1
        )

    def keys(self) -> tuple[list[Any], Any]:
        """The recipients every copy is encrypted to, and the drill identity.

        Raises ValueError, naming what is wrong but never a key, when the
        backup settings are missing or a key doesn't parse; a Key Vault error
        resolving the drill key propagates as itself. selfcheck calls it too,
        so a deploy that would fail every nightly run fails its smoke test.
        """
        from pyrage import x25519

        config = self._settings()
        missing = [
            name
            for name, value in (
                ("BACKUP_BUCKET", config.backup_bucket),
                ("BACKUP_R2_ENDPOINT", config.backup_r2_endpoint),
                ("BACKUP_AGE_RECIPIENT", config.backup_age_recipient),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"not configured, {', '.join(missing)} unset")
        try:
            recipient = x25519.Recipient.from_str(config.backup_age_recipient)
        except Exception:
            raise ValueError("BACKUP_AGE_RECIPIENT is not an age public key") from None
        # Resolved outside the parse's try, so a Key Vault error (no access,
        # not set, unreachable) is reported as itself, not as a malformed key.
        drill_key = self.secret(DRILL_KEY).strip()
        try:
            drill = x25519.Identity.from_str(drill_key)
        except Exception:
            raise ValueError(f"{DRILL_KEY} is not an age secret key") from None
        return [recipient, drill.to_public()], drill

    def run(self, client: Any | None = None, now: datetime | None = None) -> int:
        """Back up every document and verify each copy. 0 when all is well, else 1."""
        try:
            recipients, drill = self.keys()
            client = client or self.r2_client()
        except ValueError as exc:
            logger.error("backup: %s", exc)
            return 1
        except Exception as exc:
            # Key Vault or the R2 client: logged like every other failure, so
            # the job's log says what went wrong rather than ending in a
            # traceback.
            logger.error("backup: FAIL setup: %s: %s", type(exc).__name__, str(exc)[:300])
            return 1
        bucket = self._settings().backup_bucket
        now = now or datetime.now(UTC)

        failed = 0
        for doc in self.documents:
            try:
                failed += self._back_up(client, bucket, doc, recipients, drill, now)
            except Exception as exc:
                failed += 1
                logger.error(
                    "backup: FAIL %s: %s: %s", doc.name, type(exc).__name__, str(exc)[:300]
                )

        if failed:
            logger.error("backup: %d problem(s)", failed)
            return 1
        logger.info("backup: all documents copied and verified")
        return 0

    def _back_up(
        self,
        client: Any,
        bucket: str,
        doc: Document,
        recipients: list[Any],
        drill: Any,
        now: datetime,
    ) -> int:
        """Copy one document daily (and monthly, if due) and verify each. Returns failures."""
        import pyrage

        raw = doc.load()
        if raw is None:
            if doc.required:
                logger.error("backup: FAIL %s: the document does not exist", doc.name)
                return 1
            logger.info("backup: skip %s: never written", doc.name)
            return 0

        sealed = pyrage.encrypt(raw, recipients)
        if not _copy(client, bucket, doc, self.daily_key(doc.name, now), sealed, raw, drill):
            return 1

        monthly = self.monthly_key(doc.name, now)
        try:
            if _exists(client, bucket, monthly):
                return 0
        except Exception as exc:
            logger.error(
                "backup: FAIL %s -> %s: %s: %s",
                doc.name,
                monthly,
                type(exc).__name__,
                str(exc)[:300],
            )
            return 1
        return 0 if _copy(client, bucket, doc, monthly, sealed, raw, drill) else 1


def _exists(client: Any, bucket: str, key: str) -> bool:
    found = client.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
    return any(item.get("Key") == key for item in found.get("Contents", []))


def _copy(
    client: Any, bucket: str, doc: Document, key: str, sealed: bytes, raw: bytes, drill: Any
) -> bool:
    """Upload one copy, then download, decrypt, compare and parse it. True if it restores."""
    import pyrage

    try:
        client.put_object(Bucket=bucket, Key=key, Body=sealed)
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        restored = pyrage.decrypt(body, [drill])
        if restored != raw:
            raise ValueError("the decrypted copy differs from the document")
        doc.validate(restored)
    except Exception as exc:
        logger.error(
            "backup: FAIL %s -> %s: %s: %s", doc.name, key, type(exc).__name__, str(exc)[:300]
        )
        return False
    logger.info("backup: ok %s -> %s (%d bytes, restored and parsed)", doc.name, key, len(raw))
    return True
