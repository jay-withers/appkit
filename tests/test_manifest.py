"""appkit.manifest: the installable-app manifest every app serves."""

from __future__ import annotations

import pytest

from appkit import manifest


def _build(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "name": "Health",
        "short_name": "Health",
        "colour": "#101014",
        "icons": manifest.png_icons("/icons"),
    }
    kwargs.update(overrides)
    return manifest.build(**kwargs)  # type: ignore[arg-type]


def test_an_installed_app_opens_on_its_home_page_by_default() -> None:
    assert _build()["start_url"] == "/"


def test_another_start_page_has_to_be_asked_for() -> None:
    assert _build(start_url="/strength")["start_url"] == "/strength"


def test_the_identity_stays_at_the_root_whatever_the_start_page() -> None:
    """Moving the start page must update the installed app, not fork it."""
    assert _build()["id"] == "/"
    assert _build(start_url="/strength")["id"] == "/"


OFF_ORIGIN = ["https://example.com/", "//example.com/", "/\\example.com/", "strength", ""]


@pytest.mark.parametrize("start_url", OFF_ORIGIN)
def test_a_start_page_outside_the_app_is_refused(start_url: str) -> None:
    with pytest.raises(ValueError, match="start_url"):
        _build(start_url=start_url)


@pytest.mark.parametrize("prefix", OFF_ORIGIN)
def test_an_icon_prefix_outside_the_app_is_refused(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        manifest.png_icons(prefix)


def test_the_manifest_installs_standalone_in_one_colour() -> None:
    built = _build()
    assert built["display"] == "standalone"
    assert built["scope"] == "/"
    assert built["background_color"] == built["theme_color"] == "#101014"
    assert built["name"] == built["short_name"] == "Health"


def test_png_icons_name_the_three_android_icons_under_the_prefix() -> None:
    assert manifest.png_icons("/static/icons/") == [
        {
            "src": "/static/icons/icon-192.png",
            "type": "image/png",
            "sizes": "192x192",
            "purpose": "any",
        },
        {
            "src": "/static/icons/icon-512.png",
            "type": "image/png",
            "sizes": "512x512",
            "purpose": "any",
        },
        {
            "src": "/static/icons/icon-maskable-512.png",
            "type": "image/png",
            "sizes": "512x512",
            "purpose": "maskable",
        },
    ]


def test_icons_are_passed_through_as_given() -> None:
    svg = [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}]
    assert _build(icons=iter(svg))["icons"] == svg


def test_editing_the_result_leaves_the_callers_icons_alone() -> None:
    icons = manifest.png_icons("/icons")
    built = _build(icons=icons)
    built["icons"][0]["src"] = "/elsewhere.png"  # type: ignore[index]
    assert icons[0]["src"] == "/icons/icon-192.png"
