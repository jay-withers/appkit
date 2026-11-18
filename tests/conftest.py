"""Fixtures shared by the suite.

No test may reach a real Key Vault, R2 bucket or `.env`, however the machine
running it is configured: each test runs in an empty directory, with the
Azure variables cleared and `.env`'s cache emptied.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from appkit import keyvault


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    for name in ("KEY_VAULT_URI", "AZURE_CLIENT_ID", "AZURE_KEYVAULT_TOKEN", "CSP_FORM_ACTION"):
        monkeypatch.delenv(name, raising=False)
    keyvault.dotenv.cache_clear()
    yield
    keyvault.dotenv.cache_clear()
