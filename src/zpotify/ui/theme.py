"""Color palette and shared styles. Accent themes on near-black; green default.

Every accent-derived style lives in a module attribute that callers read at
render time (``theme.ACCENT``), so :func:`apply` re-skins the whole UI by
reassigning them — no caller changes, takes effect on the next frame.
"""

from __future__ import annotations

from zpotify.term.style import Style

RGB = tuple[int, int, int]

BG = (14, 14, 16)
BG_ALT = (24, 24, 28)
FG = (222, 222, 226)
FG_DIM = (130, 130, 140)
FG_FAINT = (80, 80, 90)
WHITE = (250, 250, 250)
YELLOW = (235, 200, 80)
RED = (230, 90, 90)
BLUE = (110, 160, 235)

# Accent themes: name -> (accent, dim accent, visualizer tip). The tip is the
# gradient's bright end; None lightens the accent toward white instead.
# Green keeps its yellow tip so the default look stays exactly as it was.
THEMES: dict[str, tuple[RGB, RGB, RGB | None]] = {
    "green":   ((30, 215, 96), (24, 130, 62), (235, 200, 80)),
    "cyan":    ((80, 220, 230), (48, 132, 138), None),
    "blue":    ((100, 155, 235), (60, 93, 141), None),
    "teal":    ((60, 200, 180), (36, 120, 108), None),
    "lime":    ((165, 230, 70), (99, 138, 42), None),
    "yellow":  ((235, 200, 80), (141, 120, 48), None),
    "orange":  ((240, 150, 60), (144, 90, 36), None),
    "red":     ((230, 85, 85), (138, 51, 51), None),
    "pink":    ((240, 120, 175), (144, 72, 105), None),
    "magenta": ((225, 85, 225), (135, 51, 135), None),
    "purple":  ((170, 120, 240), (102, 72, 144), None),
    "white":   ((245, 245, 245), (150, 150, 154), None),
}
DEFAULT_THEME = "green"

BASE = Style(fg=FG, bg=BG)
DIM = Style(fg=FG_DIM, bg=BG)
FAINT = Style(fg=FG_FAINT, bg=BG)
TITLE = Style(fg=WHITE, bg=BG, bold=True)
ERROR = Style(fg=RED, bg=BG, bold=True)

TAB_INACTIVE = Style(fg=FG_DIM, bg=BG_ALT)

ROW = Style(fg=FG, bg=BG)
ROW_ALT = Style(fg=FG, bg=BG)
ROW_DIM = Style(fg=FG_DIM, bg=BG)

BAR_TODO = Style(fg=(52, 52, 58), bg=BG)

INPUT_FG = (200, 200, 205)  # light grey — readable in truecolor and 256 modes
INPUT = Style(fg=INPUT_FG, bg=BG_ALT)
INPUT_FOCUS = Style(fg=INPUT_FG, bg=(34, 34, 40))


def _lerp(a: RGB, b: RGB, t: float) -> RGB:
    return (int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def apply(name: str) -> None:
    """Switch every accent-derived style to the named theme (unknown -> default)."""
    global THEME, ACCENT_RGB, ACCENT_DIM_RGB, _TIP_RGB
    global ACCENT, ACCENT_BOLD, TAB_ACTIVE, ROW_SELECTED, ROW_DIM_SELECTED, BAR_DONE
    THEME = name if name in THEMES else DEFAULT_THEME
    accent, dim, tip = THEMES[THEME]
    ACCENT_RGB, ACCENT_DIM_RGB = accent, dim
    _TIP_RGB = tip or _lerp(accent, (255, 255, 255), 0.7)
    selected_bg = _lerp(_lerp(BG, accent, 0.2), (255, 255, 255), 0.06)

    ACCENT = Style(fg=accent, bg=BG)
    ACCENT_BOLD = Style(fg=accent, bg=BG, bold=True)
    TAB_ACTIVE = Style(fg=(10, 10, 12), bg=accent, bold=True)
    ROW_SELECTED = Style(fg=WHITE, bg=selected_bg, bold=True)
    ROW_DIM_SELECTED = Style(fg=FG_DIM, bg=selected_bg)
    BAR_DONE = Style(fg=accent, bg=BG)


def spectrum_color(height: float) -> RGB:
    """Gradient for visualizer bars: dim accent -> accent -> bright tip."""
    if height < 0.6:
        return _lerp(ACCENT_DIM_RGB, ACCENT_RGB, height / 0.6)
    return _lerp(ACCENT_RGB, _TIP_RGB, (height - 0.6) / 0.4)


apply(DEFAULT_THEME)
