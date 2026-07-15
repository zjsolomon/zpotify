"""Tests for color-mode detection and the xterm-256 quantizer."""

from __future__ import annotations

import pytest

from zpotify.term import style as style_mod
from zpotify.term.style import Style, detect_color_mode, set_color_mode, xterm256


@pytest.fixture(autouse=True)
def _restore_mode():
    yield
    set_color_mode("truecolor")


def test_detect_color_mode(monkeypatch) -> None:
    monkeypatch.delenv("ZPOTIFY_COLOR", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert detect_color_mode() == "truecolor"
    monkeypatch.setenv("COLORTERM", "24bit")
    assert detect_color_mode() == "truecolor"
    monkeypatch.delenv("COLORTERM")
    assert detect_color_mode() == "256"  # Apple Terminal: no COLORTERM
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("ZPOTIFY_COLOR", "256")
    assert detect_color_mode() == "256"  # explicit override wins


def test_quantizer_known_values() -> None:
    assert xterm256((0, 0, 0)) == 16          # cube black
    assert xterm256((255, 255, 255)) == 231   # cube white
    assert xterm256((255, 0, 0)) == 196       # saturated red -> cube corner
    assert xterm256((0, 255, 0)) == 46
    assert xterm256((0, 0, 255)) == 21
    assert xterm256((8, 8, 8)) == 232         # greyscale ramp start
    assert xterm256((238, 238, 238)) == 255   # greyscale ramp end
    assert xterm256((128, 128, 128)) == 244   # mid grey -> ramp, not cube


def test_sgr_emits_indexed_colors_in_256_mode() -> None:
    s = Style(fg=(30, 215, 96), bg=(14, 14, 16), bold=True)
    set_color_mode("truecolor")
    assert "38;2;30;215;96" in s.sgr()
    set_color_mode("256")
    out = s.sgr()
    assert "38;5;" in out and "48;5;" in out
    assert "38;2;" not in out and "48;2;" not in out
    assert out.startswith("\x1b[0;1;")  # attributes preserved


def test_theme_styles_keep_contrast_in_256_mode() -> None:
    """No theme style may quantize fg and bg to the same palette index."""
    from zpotify.ui import theme

    for name in dir(theme):
        value = getattr(theme, name)
        if isinstance(value, Style) and value.fg is not None and value.bg is not None:
            assert xterm256(value.fg) != xterm256(value.bg), (
                f"theme.{name}: fg {value.fg} and bg {value.bg} collapse to "
                f"the same 256-color index {xterm256(value.fg)}"
            )
