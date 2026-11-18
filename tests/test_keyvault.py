"""Secret resolution (keyvault.py): the environment, then `.env`, then the vault."""

from __future__ import annotations

import time
from typing import ClassVar

import azure.identity
import azure.keyvault.secrets
import pytest
from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError

from appkit import keyvault
from appkit.keyvault import StaticTokenCredential, make_credential, resolve, resolve_optional

VAULT = "https://kv-app-dev.vault.azure.net/"


def _no_credential() -> object:
    raise AssertionError("the vault was consulted")


def test_a_secret_comes_from_the_environment_first(monkeypatch):
    monkeypatch.setenv("APP_PASSCODE", "from-env")
    assert resolve("APP-PASSCODE", key_vault_uri=VAULT, credential=_no_credential) == "from-env"


def test_hyphens_map_to_underscores(monkeypatch):
    """Key Vault forbids underscores, env vars conventionally forbid hyphens."""
    monkeypatch.setenv("SOME_OTHER_VALUE", "x")
    assert keyvault.from_environment("SOME-OTHER-VALUE") == "x"


def test_a_missing_secret_with_no_vault_says_what_to_do(monkeypatch):
    monkeypatch.delenv("APP_PASSCODE", raising=False)
    with pytest.raises(RuntimeError, match=r"\$APP_PASSCODE.*az keyvault secret set"):
        resolve("APP-PASSCODE", key_vault_uri="", credential=_no_credential)


def test_an_optional_secret_is_allowed_to_be_absent(monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    assert resolve_optional("NOT-SET-ANYWHERE", key_vault_uri="", credential=_no_credential) is None


# --- Key Vault ---------------------------------------------------------------


class _FakeSecret:
    def __init__(self, value: str | None) -> None:
        self.value = value


class _FakeVault:
    """The `SecretClient` surface used, plus a record of how it was built."""

    built: ClassVar[list[_FakeVault]] = []
    secrets: ClassVar[dict[str, str | None]] = {}
    raises: ClassVar[Exception | None] = None

    def __init__(self, vault_url: str, credential: object) -> None:
        self.vault_url = vault_url
        self.credential = credential
        self.requested: list[str] = []
        _FakeVault.built.append(self)

    def get_secret(self, name: str) -> _FakeSecret:
        self.requested.append(name)
        if self.raises is not None:
            raise self.raises
        if name not in self.secrets:
            raise ResourceNotFoundError(f"no secret named {name}")
        return _FakeSecret(self.secrets[name])


@pytest.fixture
def vault(monkeypatch):
    monkeypatch.delenv("APP_PASSCODE", raising=False)
    monkeypatch.setattr(azure.keyvault.secrets, "SecretClient", _FakeVault)
    monkeypatch.setattr(_FakeVault, "built", [])
    monkeypatch.setattr(_FakeVault, "secrets", {"APP-PASSCODE": "from-the-vault", "EMPTY": None})
    monkeypatch.setattr(_FakeVault, "raises", None)
    return _FakeVault.built


def test_a_secret_falls_through_to_the_vault(vault):
    credential = StaticTokenCredential("a-token")
    assert resolve("APP-PASSCODE", key_vault_uri=VAULT, credential=lambda: credential) == (
        "from-the-vault"
    )
    assert vault[0].vault_url == VAULT
    assert vault[0].credential is credential
    # The name goes to the vault as it stands: only the environment lookup
    # rewrites it.
    assert vault[0].requested == ["APP-PASSCODE"]


def test_the_environment_still_wins_over_a_configured_vault(vault, monkeypatch):
    monkeypatch.setenv("APP_PASSCODE", "from-env")
    assert resolve("APP-PASSCODE", key_vault_uri=VAULT, credential=_no_credential) == "from-env"
    assert vault == []


def test_a_required_secret_the_vault_holds_empty_is_empty(vault):
    assert resolve("EMPTY", key_vault_uri=VAULT, credential=object) == ""


def test_an_optional_secret_missing_from_the_vault_is_none(vault):
    assert resolve_optional("NOT-IN-THE-VAULT", key_vault_uri=VAULT, credential=object) is None
    assert resolve_optional("EMPTY", key_vault_uri=VAULT, credential=object) is None


def test_an_optional_secret_in_the_vault_is_returned(vault):
    assert resolve_optional("APP-PASSCODE", key_vault_uri=VAULT, credential=object) == (
        "from-the-vault"
    )


def test_a_broken_credential_is_not_reported_as_an_absent_secret(vault, monkeypatch):
    """ "Not configured" returns None; "cannot authenticate" is a fault and raises."""
    monkeypatch.setattr(_FakeVault, "raises", ClientAuthenticationError("no managed identity"))
    with pytest.raises(ClientAuthenticationError):
        resolve_optional("APP-PASSCODE", key_vault_uri=VAULT, credential=object)


# --- the credential ----------------------------------------------------------


def test_a_pre_fetched_token_wins():
    """The containerised local loop: the image has no `az` to fall back to."""
    credential = make_credential(keyvault_token="a-token", client_id="an-identity")
    assert isinstance(credential, StaticTokenCredential)


def test_the_identity_is_named_explicitly_when_there_is_one(monkeypatch):
    """`DefaultAzureCredential` picks unpredictably when several are attached."""
    named: list[str] = []
    monkeypatch.setattr(
        azure.identity, "ManagedIdentityCredential", lambda client_id: named.append(client_id)
    )
    make_credential(keyvault_token="", client_id="the-user-assigned-identity")
    assert named == ["the-user-assigned-identity"]


def test_it_falls_back_to_the_developers_az_login(monkeypatch):
    built: list[str] = []
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda: built.append("default"))
    make_credential(keyvault_token="", client_id="")
    assert built == ["default"]


def test_the_static_token_refuses_a_scope_it_was_not_issued_for():
    credential = StaticTokenCredential("a-token")
    assert credential.get_token(keyvault.KEY_VAULT_SCOPE).token == "a-token"
    assert credential.get_token_info(keyvault.KEY_VAULT_SCOPE).token == "a-token"
    with pytest.raises(ValueError, match="scoped to"):
        credential.get_token("https://storage.azure.com/.default")
    with pytest.raises(ValueError, match="scoped to"):
        credential.get_token_info("https://storage.azure.com/.default")


def test_the_static_token_carries_an_expiry_the_sdk_will_accept():
    """Zero would make every SDK call treat it as expired before sending it."""
    assert StaticTokenCredential("a-token").get_token().expires_on > time.time()


# --- .env --------------------------------------------------------------------


def test_a_secret_comes_from_dotenv_when_the_environment_is_silent(monkeypatch, tmp_path):
    """`.env` belongs to whoever runs the command, so it is read from the cwd."""
    (tmp_path / ".env").write_text("APP_PASSCODE=from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv("APP_PASSCODE", raising=False)
    assert resolve("APP-PASSCODE", key_vault_uri="", credential=_no_credential) == "from-dotenv"


def test_the_real_environment_overrides_a_checked_out_dotenv(monkeypatch, tmp_path):
    """`APP_PASSCODE=x make run` must not be silently ignored by a `.env` on disk."""
    (tmp_path / ".env").write_text("APP_PASSCODE=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("APP_PASSCODE", "from-env")
    assert keyvault.from_environment("APP-PASSCODE") == "from-env"


def test_no_dotenv_is_not_an_error():
    """There is none in the image, and that is the ordinary case rather than a fault."""
    assert keyvault.dotenv() == {}
