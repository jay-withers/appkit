"""Secret resolution: the environment, then `.env`, then Key Vault.

Was `settings.py`'s machinery, copied from market-agent to repo-agent, gym-log,
finances and career. The app keeps its own `Settings` class and thin `secret()`
/ `optional_secret()` / `credential()` wrappers (cached there, so a test can
clear them); the logic lives here once.

Every secret is read from an environment variable first, then `.env`, and only
then from Key Vault. That ordering is what makes `make run` work locally with no
Azure involved, and it is why Terraform manages no Container Apps Key Vault
reference: a revision carrying one hard-fails if the secret is absent, whereas
this resolves at runtime and reports what is missing.

The name mapping is mechanical: `resolve("APP-PASSCODE", ...)` reads
`$APP_PASSCODE`, then `.env`, then the Key Vault secret named `APP-PASSCODE`.
Key Vault forbids underscores in names, environment variables conventionally
forbid hyphens, so one of the two has to be rewritten.

The Azure SDK is imported inside each function, so an app's model, its pure
logic and its tests never need it installed or a credential, and a test can
fake `azure.keyvault.secrets.SecretClient` at the module.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any

# The scope a Key Vault data-plane token is issued for. `az` calls the same
# thing `--resource https://vault.azure.net`.
KEY_VAULT_SCOPE = "https://vault.azure.net/.default"


@lru_cache(maxsize=1)
def dotenv() -> dict[str, str]:
    """`.env` as a plain dict, or empty when there is no such file.

    Read with python-dotenv, the parser pydantic-settings uses, so a value
    quoted for an app's `Settings` is quoted the same here.

    Resolved relative to the working directory rather than to this file: `.env`
    belongs to whoever is running the command, and in the image there is none.
    """
    from dotenv import dotenv_values

    return {key: value for key, value in dotenv_values(".env").items() if value is not None}


def from_environment(name: str) -> str | None:
    """A secret from the process environment, then `.env`.

    The real environment wins, so an inline `FOO=bar make run` overrides a
    checked-out `.env` rather than being silently ignored by it.
    """
    variable = name.replace("-", "_").upper()
    return os.environ.get(variable) or dotenv().get(variable)


def resolve(name: str, *, key_vault_uri: str, credential: Callable[[], Any]) -> str:
    """Resolve a required secret by its hyphenated Key Vault name.

    Raises RuntimeError, saying how to set it, when it is in neither the
    environment nor `.env` and no vault is configured. `credential` is called
    only when the vault is actually consulted.
    """
    from_env = from_environment(name)
    if from_env:
        return from_env

    if not key_vault_uri:
        raise RuntimeError(
            f"{name} is not set and no KEY_VAULT_URI is configured. "
            f"Set ${name.replace('-', '_').upper()} locally, or "
            f"`az keyvault secret set --name {name}` for a deployed environment."
        )

    from azure.keyvault.secrets import SecretClient

    client = SecretClient(vault_url=key_vault_uri, credential=credential())
    return client.get_secret(name).value or ""


def resolve_optional(name: str, *, key_vault_uri: str, credential: Callable[[], Any]) -> str | None:
    """Resolve a secret that is allowed not to exist, returning None if it does not.

    **Absence is not the same as failure.** A missing env var, no vault
    configured, or a secret that is not in the vault all return None; an
    authentication or network error propagates, because "the credential is
    broken" must not look like "not configured".
    """
    from_env = from_environment(name)
    if from_env:
        return from_env

    if not key_vault_uri:
        return None

    from azure.core.exceptions import ResourceNotFoundError
    from azure.keyvault.secrets import SecretClient

    client = SecretClient(vault_url=key_vault_uri, credential=credential())
    try:
        return client.get_secret(name).value or None
    except ResourceNotFoundError:
        return None


class StaticTokenCredential:
    """A credential wrapping one pre-fetched access token.

    Exists for the containerised local loop. The application image carries no
    `az`, so `DefaultAzureCredential` has nothing to fall back to. Passing a
    token in is strictly better than writing secrets to a `.env`: it expires in
    about an hour and is scoped to Key Vault alone.

    Never used in Azure, where the managed identity is available directly.
    """

    def __init__(self, token: str, scope: str = KEY_VAULT_SCOPE) -> None:
        self._token = token
        self._scope = scope
        # az does not report the expiry in a form worth parsing here, and the
        # SDK only uses this to decide whether to refresh, which this
        # credential cannot do. An hour matches the real lifetime; an expired
        # token then fails as a 401 from Key Vault, which is the honest outcome.
        self._expires_on = int(time.time()) + 3600

    def _check(self, scopes: tuple[str, ...]) -> None:
        """Refuse a scope this token was not issued for."""
        if scopes and self._scope not in scopes:
            raise ValueError(f"this token is scoped to {self._scope}, not {', '.join(scopes)}")

    def get_token(self, *scopes: str, **_kwargs: Any) -> Any:
        from azure.core.credentials import AccessToken

        self._check(scopes)
        return AccessToken(self._token, self._expires_on)

    def get_token_info(self, *scopes: str, **_kwargs: Any) -> Any:
        """The newer protocol; some SDK versions call this instead."""
        from azure.core.credentials import AccessTokenInfo

        self._check(scopes)
        return AccessTokenInfo(self._token, self._expires_on)


def make_credential(*, keyvault_token: str, client_id: str) -> Any:
    """The credential for Key Vault and Blob Storage.

    Three cases, in priority order:

    1. A pre-fetched Key Vault token (`AZURE_KEYVAULT_TOKEN`): the
       containerised local loop, where there is no `az` to fall back to.
    2. The user-assigned identity (`AZURE_CLIENT_ID`), named explicitly. In
       Azure this must be explicit: `DefaultAzureCredential` would find the
       same identity eventually, but a workload with more than one identity
       attached picks unpredictably, and the failure looks like a permissions
       problem rather than a wrong-identity one.
    3. `DefaultAzureCredential`, which picks up a developer's `az login`.
    """
    if keyvault_token:
        return StaticTokenCredential(keyvault_token)

    if client_id:
        from azure.identity import ManagedIdentityCredential

        return ManagedIdentityCredential(client_id=client_id)

    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()
