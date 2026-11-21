# appkit

Shared building blocks for the jay-withers apps on the shared Container Apps
environment (career, finances, health). Each module was once copied between
those apps, and a fix had to be made in every copy. It's now made here once,
and each app picks it up with a version bump.

| Module | What it is | Extra |
| --- | --- | --- |
| `appkit.headers` | Security headers and the per-response CSP script nonce: `install(app)` | `web` |
| `appkit.health` | `/healthz` (from memory, reporting `IMAGE_TAG` as `version`) and `/readyz` (the app's own `ready()`, 503 when it raises): `install(app, environment=, ready=)` | `web` |
| `appkit.manifest` | The home-screen install manifest: `build(name=, short_name=, colour=, icons=)`, opening on `/` unless `start_url=` says otherwise, with `id` pinned to `/`; `png_icons(prefix)` names the three Android PNGs | none |
| `appkit.passcode` | The shared-passcode gate: `PasscodeGate(cookie_name, settings=, secret=)`, with `issue`, `valid`, `check_passcode` and the `require_session` dependency | `web` |
| `appkit.keyvault` | Secret resolution (environment, then `.env`, then Key Vault) and the Azure credential | `azure` |
| `appkit.backup` | The nightly age-encrypted copy of each document to Cloudflare R2, verified by restoring it | `backup` |
| `appkit.integrity` | Does a stored document survive this code's rewrite (`roundtrip`), and pass the app's own consistency `check`? `require` raises `IntegrityError`, naming JSON paths but never values | none |
| `appkit.selfcheck` | The post-deploy check loop, plus secret, backup and integrity checks | none |
| `appkit.testing` | Browser tests: `live_server` runs the app on a free port, `log_in` signs in through `/login`, `watch_console` collects console errors (a CSP block is one), and `require_browser` skips without Chromium locally but fails under CI. In the app's dev extra, not its runtime | `e2e` |

Each app keeps what is really its own: its `Settings` class, its list of
documents and secrets, and thin cached wrappers (`secret()`, `credential()`)
that its tests can clear. appkit reads settings through small protocols
(`PasscodeSettings`, `BackupSettings`) whose field names match the apps'
existing settings.

## Using it

Pin a release tag in the app's `pyproject.toml`, with the extras it needs:

```toml
dependencies = [
  "appkit[azure,backup,web]",
]

[tool.uv.sources]
appkit = { git = "https://github.com/jay-withers/appkit", tag = "v0.2.0" }
```

Pin it in `[tool.uv.sources]`, not as `appkit @ git+https://...` in
`dependencies`: Renovate bumps a uv git source's tag (running `uv lock`), but
skips a PEP 508 direct reference.

Extras: `web` (FastAPI) for `headers` and `passcode`, `azure` for `keyvault`,
`backup` for running a backup. The core has no dependencies, so a batch job
that only resolves secrets takes `appkit[azure]` and no web framework.

The repo is public, so this needs no token. uv fetches a git source with the
`git` CLI, so the image builder needs `git` (dev-containers' python-app-builder
has it from v0.22.0).

```python
from appkit import headers
from appkit.passcode import PasscodeGate

from .settings import secret, settings

gate = PasscodeGate("gymlog_session", settings=settings, secret=secret)
headers.install(app)
```

## Developing

Once per machine, set up the tools with
[jay-withers/toolchain](https://github.com/jay-withers/toolchain). Then:

```bash
make install     # mise tools, pre-commit hooks, and the venv with every extra
make test        # pytest
make typecheck   # mypy over src/ and tests/
make lint        # every pre-commit hook (ruff included)
```

CI (`ci-python`) runs the suite and mypy on Python 3.14, the apps' runtime,
and gates diff coverage at 80%. `cd-tag` tags a release on every merge to
`main` from the Conventional Commits since the last one, and Renovate raises
the pin in each app.

## Changing it

A change here reaches every app on its next bump, so:

- **Keep behaviour compatible**, or make it a `feat!:` and bump every app in
  the same sweep.
- **Every fix gets a regression test here.** The apps' own tests keep
  covering how they use appkit.
- **Nothing logs a secret's value**, and nothing imports the Azure SDK, boto3
  or pyrage at module level: an app's pure code and tests must not need them.
