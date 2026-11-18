"""The core and the non-web extras must import without FastAPI installed."""

import subprocess
import sys

# A None entry in sys.modules makes `import fastapi` raise ImportError, which
# is what a job installing `appkit[azure]` (no `web` extra) would hit.
PROBE = """
import sys
sys.modules["fastapi"] = None
sys.modules["starlette"] = None
import appkit, appkit.keyvault, appkit.backup, appkit.selfcheck, appkit.testing
"""


def test_non_web_modules_import_without_fastapi() -> None:
    result = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True)  # noqa: S603
    assert result.returncode == 0, result.stderr


def test_web_modules_do_need_fastapi() -> None:
    """The probe is real: it does break the modules that import FastAPI."""
    probe = PROBE.replace("appkit.selfcheck", "appkit.selfcheck, appkit.headers")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)  # noqa: S603
    assert result.returncode != 0
    assert "fastapi" in result.stderr
