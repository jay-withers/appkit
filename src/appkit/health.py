"""Liveness and readiness: `/healthz` and `/readyz`.

Was identical in career, finances and gym-log; this is now the one copy. An
app calls `install(app, environment=..., ready=...)` once.

**`/healthz` and `/readyz` are different on purpose.** Container Apps' liveness
probe restarts a replica that fails it, so `/healthz` answers from memory
alone: a probe that touched storage would turn a blob blip into a restart
loop. `/readyz` is the one that reaches storage, by calling the app's own
`ready()`, and answers 503 when that raises.

**`/healthz` reports `version`**, the image tag `make deploy` sets as
`IMAGE_TAG`. The shared smoke test (jay-withers/workflows' scripts/smoke.sh)
waits for it to equal the tag just deployed, which is what proves the new
revision is the one serving rather than merely accepted by Azure.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Response, status

logger = logging.getLogger(__name__)


def install(app: FastAPI, *, environment: str, ready: Callable[[], object]) -> None:
    """Add `/healthz` and `/readyz` to `app`, both outside any session gate.

    `ready` should do the cheapest read that proves this replica can serve,
    such as loading the app's document. It is called on every `/readyz`, and
    any exception it raises makes the answer a 503 (logged, never returned:
    the message may name a storage account).
    """

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "environment": environment,
            "version": os.environ.get("IMAGE_TAG", "unknown"),
        }

    @app.get("/readyz", include_in_schema=False)
    def readyz(response: Response) -> dict[str, Any]:
        try:
            ready()
        except Exception as exc:
            logger.warning("readiness check failed: %s", exc)
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unavailable", "storage": False}
        return {"status": "ok", "storage": True}
