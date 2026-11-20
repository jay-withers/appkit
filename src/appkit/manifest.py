"""The web app manifest that lets a phone install an app to its home screen.

Was written out by hand in career, finances and gym-log; this
is now the one shape. An app serves `build(...)` from a public
`/manifest.json` (a fresh install starts on the login page, before any cookie
exists).

**Every app opens on its home page.** `start_url` is `/` unless an app passes
another, so an exception is an argument a reviewer can see. gym-log once
opened its installed app on one section (`/strength`) after it grew a home
page, and that choice sat in a docstring, the README and a test without ever
being put to its owner.

**`id` is always `/`.** It pins the installed app's identity apart from
`start_url`, so moving the start page updates the installed app rather than
counting as a different one. Chrome derives a missing `id` from `start_url`,
so an app that had no `id` and started at `/` keeps the same identity.

`display: standalone` drops the browser bar, which is the point of
installing. Android builds the installed app from a raster icon, so
`png_icons` names the three PNGs every app renders: 192 and 512 for any
purpose, and a maskable 512 whose glyph sits inside the middle 80% the
launcher may crop to.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

PNG_ICONS = (
    ("icon-192.png", "192x192", "any"),
    ("icon-512.png", "512x512", "any"),
    ("icon-maskable-512.png", "512x512", "maskable"),
)
"""(filename, sizes, purpose) for each PNG icon, for an app's icon route too."""


def _require_local_path(what: str, value: str) -> None:
    """Refuse anything a browser would resolve off this app's origin or
    relative to the manifest's own URL: an absolute URL, `//host` and
    `/\\host` (which browsers read as `//host`), or a bare relative path."""
    if not value.startswith("/") or value[1:2] in ("/", "\\"):
        raise ValueError(f"{what} must be a path inside the app, not {value!r}")


def png_icons(prefix: str) -> list[dict[str, str]]:
    """The manifest entries for `PNG_ICONS` served under `prefix`, such as
    `/icons` or `/static/icons`."""
    _require_local_path("prefix", prefix)
    base = prefix.rstrip("/")
    return [
        {"src": f"{base}/{name}", "type": "image/png", "sizes": sizes, "purpose": purpose}
        for name, sizes, purpose in PNG_ICONS
    ]


def build(
    *,
    name: str,
    short_name: str,
    colour: str,
    icons: Iterable[dict[str, str]],
    start_url: str = "/",
) -> dict[str, Any]:
    """The manifest as a dict, ready to return from a FastAPI route.

    `colour` is both the splash background and the theme (the status bar),
    which every app keeps the same as its page background.
    """
    _require_local_path("start_url", start_url)
    return {
        "id": "/",
        "name": name,
        "short_name": short_name,
        "start_url": start_url,
        "scope": "/",
        "display": "standalone",
        "background_color": colour,
        "theme_color": colour,
        # Copies, so editing the result never reaches the caller's icon list.
        "icons": [dict(icon) for icon in icons],
    }
