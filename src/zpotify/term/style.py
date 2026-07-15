"""Cell styling for the terminal renderer.

Colors are authored as 24-bit RGB. Terminals that support truecolor
(COLORTERM=truecolor/24bit — iTerm2, Ghostty, Kitty, WezTerm) get exact
`38;2;R;G;B` codes; everything else (notably Apple Terminal.app, which mangles
truecolor SGRs) gets the nearest xterm-256 color via `38;5;N`. The mode is
detected once at Screen.enter(); override with ZPOTIFY_COLOR=truecolor|256.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass, replace

RGB = tuple[int, int, int]

_COLOR_MODE = "truecolor"  # module-global; set via set_color_mode()

# xterm 6x6x6 color-cube channel levels (indices 16..231).
_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)


def detect_color_mode() -> str:
    """Pick the color mode for this terminal (env override first)."""
    forced = os.environ.get("ZPOTIFY_COLOR", "").lower()
    if forced in ("truecolor", "256"):
        return forced
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return "truecolor"
    return "256"


def set_color_mode(mode: str) -> None:
    global _COLOR_MODE
    if mode not in ("truecolor", "256"):
        raise ValueError(f"unknown color mode: {mode!r}")
    _COLOR_MODE = mode


def color_mode() -> str:
    return _COLOR_MODE


def _nearest_cube_level(v: int) -> int:
    return min(range(6), key=lambda i: abs(_CUBE_LEVELS[i] - v))


@functools.lru_cache(maxsize=4096)
def xterm256(rgb: RGB) -> int:
    """Nearest xterm-256 palette index for an RGB color.

    Considers both the 6x6x6 cube (16..231) and the 24-step greyscale ramp
    (232..255, levels 8, 18, ... 238) and returns whichever is closer.
    """
    r, g, b = rgb
    ri, gi, bi = _nearest_cube_level(r), _nearest_cube_level(g), _nearest_cube_level(b)
    cr, cg, cb = _CUBE_LEVELS[ri], _CUBE_LEVELS[gi], _CUBE_LEVELS[bi]
    cube_dist = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
    cube_index = 16 + 36 * ri + 6 * gi + bi

    grey_avg = (r + g + b) // 3
    gi24 = max(0, min(23, (grey_avg - 8 + 5) // 10))
    grey = 8 + 10 * gi24
    grey_dist = (r - grey) ** 2 + (g - grey) ** 2 + (b - grey) ** 2
    grey_index = 232 + gi24

    return grey_index if grey_dist < cube_dist else cube_index


@dataclass(frozen=True)
class Style:
    fg: RGB | None = None
    bg: RGB | None = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False

    def with_(self, **kw) -> "Style":
        return replace(self, **kw)

    def sgr(self) -> str:
        """ANSI SGR sequence that switches the terminal to this style (from reset)."""
        parts = ["0"]
        if self.bold:
            parts.append("1")
        if self.dim:
            parts.append("2")
        if self.italic:
            parts.append("3")
        if self.underline:
            parts.append("4")
        if self.reverse:
            parts.append("7")
        if _COLOR_MODE == "truecolor":
            if self.fg is not None:
                parts.append(f"38;2;{self.fg[0]};{self.fg[1]};{self.fg[2]}")
            if self.bg is not None:
                parts.append(f"48;2;{self.bg[0]};{self.bg[1]};{self.bg[2]}")
        else:
            if self.fg is not None:
                parts.append(f"38;5;{xterm256(self.fg)}")
            if self.bg is not None:
                parts.append(f"48;5;{xterm256(self.bg)}")
        return "\x1b[" + ";".join(parts) + "m"


DEFAULT = Style()
