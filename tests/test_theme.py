"""Tests for accent themes: apply(), gradient endpoints, persistence defaults."""

from __future__ import annotations

import pytest

from zpotify.term.style import Style, xterm256
from zpotify.ui import theme


@pytest.fixture(autouse=True)
def _restore_default_theme():
    yield
    theme.apply(theme.DEFAULT_THEME)


def test_apply_reskins_accent_styles() -> None:
    accent, dim, _ = theme.THEMES["red"]
    theme.apply("red")
    assert theme.THEME == "red"
    assert theme.ACCENT.fg == accent
    assert theme.ACCENT_BOLD.fg == accent and theme.ACCENT_BOLD.bold
    assert theme.TAB_ACTIVE.bg == accent
    assert theme.BAR_DONE.fg == accent
    assert theme.ACCENT_DIM_RGB == dim


def test_unknown_theme_falls_back_to_default() -> None:
    theme.apply("hotdog")
    assert theme.THEME == theme.DEFAULT_THEME
    assert theme.ACCENT.fg == theme.THEMES[theme.DEFAULT_THEME][0]


def test_default_green_gradient_unchanged() -> None:
    """The classic look: dim green -> spotify green -> yellow tip."""
    theme.apply("green")
    assert theme.spectrum_color(0.0) == (24, 130, 62)
    assert theme.spectrum_color(0.6) == (30, 215, 96)
    assert theme.spectrum_color(1.0) == (235, 200, 80)


def test_gradient_tracks_theme_accent() -> None:
    accent, dim, _ = theme.THEMES["blue"]
    theme.apply("blue")
    assert theme.spectrum_color(0.0) == dim
    assert theme.spectrum_color(0.6) == accent


def test_all_themes_keep_contrast_in_256_mode() -> None:
    """No theme may quantize any style's fg and bg to the same palette index."""
    for name in theme.THEMES:
        theme.apply(name)
        for attr in dir(theme):
            value = getattr(theme, attr)
            if isinstance(value, Style) and value.fg is not None and value.bg is not None:
                assert xterm256(value.fg) != xterm256(value.bg), (
                    f"theme {name}: {attr} fg {value.fg} and bg {value.bg} "
                    f"collapse to 256-color index {xterm256(value.fg)}"
                )
