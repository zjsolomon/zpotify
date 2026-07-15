"""Color palette and shared styles. Spotify-green on near-black."""

from __future__ import annotations

from zpotify.term.style import Style

GREEN = (30, 215, 96)
GREEN_DIM = (24, 130, 62)
BG = (14, 14, 16)
BG_ALT = (24, 24, 28)
FG = (222, 222, 226)
FG_DIM = (130, 130, 140)
FG_FAINT = (80, 80, 90)
WHITE = (250, 250, 250)
YELLOW = (235, 200, 80)
RED = (230, 90, 90)
BLUE = (110, 160, 235)

BASE = Style(fg=FG, bg=BG)
DIM = Style(fg=FG_DIM, bg=BG)
FAINT = Style(fg=FG_FAINT, bg=BG)
TITLE = Style(fg=WHITE, bg=BG, bold=True)
ACCENT = Style(fg=GREEN, bg=BG)
ACCENT_BOLD = Style(fg=GREEN, bg=BG, bold=True)
ERROR = Style(fg=RED, bg=BG, bold=True)

TAB_ACTIVE = Style(fg=(10, 10, 12), bg=GREEN, bold=True)
TAB_INACTIVE = Style(fg=FG_DIM, bg=BG_ALT)

ROW = Style(fg=FG, bg=BG)
ROW_ALT = Style(fg=FG, bg=BG)
ROW_SELECTED = Style(fg=WHITE, bg=(40, 60, 46), bold=True)
ROW_DIM = Style(fg=FG_DIM, bg=BG)
ROW_DIM_SELECTED = Style(fg=FG_DIM, bg=(40, 60, 46))

BAR_DONE = Style(fg=GREEN, bg=BG)
BAR_TODO = Style(fg=(52, 52, 58), bg=BG)

INPUT_FG = (200, 200, 205)  # light grey — readable in truecolor and 256 modes
INPUT = Style(fg=INPUT_FG, bg=BG_ALT)
INPUT_FOCUS = Style(fg=INPUT_FG, bg=(34, 34, 40))


def spectrum_color(height: float) -> tuple[int, int, int]:
    """Gradient for visualizer bars: green -> bright green -> yellow tip."""
    if height < 0.6:
        t = height / 0.6
        return (int(24 + t * 6), int(130 + t * 85), int(62 + t * 34))
    t = (height - 0.6) / 0.4
    return (int(30 + t * 205), int(215 - t * 15), int(96 - t * 16))
