"""The zpotify 8-bit wordmark, rendered with half-block characters.

Same pixel font as the zwisp project's logo (9 rows: 2 ascender rows, 5-row
x-height body, 2 descender rows). Two pixel rows share one terminal cell via
▀/▄/█, so the wordmark is ceil(9/2) = 5 terminal rows tall. The dot of the
'i' takes the accent color (zwisp accents its i-dot the same way).
"""

from __future__ import annotations

from zpotify.term.screen import Screen
from zpotify.term.style import RGB, Style

FONT_ROWS = 9

GLYPHS = {
    "z": [".....", ".....",
          "#####", "...#.", "..#..", ".#...", "#####",
          ".....", "....."],
    "p": [".....", ".....",
          "####.", "#...#", "#...#", "####.", "#....",
          "#....", "#...."],
    "o": [".....", ".....",
          ".###.", "#...#", "#...#", "#...#", ".###.",
          ".....", "....."],
    "t": ["#..", "#..",
          "###", "#..", "#..", "#..", ".##",
          "...", "..."],
    "i": ["#", ".",
          "#", "#", "#", "#", "#",
          ".", "."],
    "f": [".##", "#..",
          "###", "#..", "#..", "#..", "#..",
          "...", "..."],
    "y": [".....", ".....",
          "#...#", "#...#", "#...#", ".####", "....#",
          "....#", ".###."],
}

WORD = "zpotify"
ACCENT_ROW = 0  # the i-dot lives in pixel row 0


def _pixel_grid() -> list[list[int]]:
    """0 = off, 1 = body pixel, 2 = accent pixel (the i dot)."""
    widths = [len(GLYPHS[c][2]) for c in WORD]
    total = sum(widths) + len(WORD) - 1
    grid = [[0] * total for _ in range(FONT_ROWS)]
    cx = 0
    for c in WORD:
        glyph = GLYPHS[c]
        gw = len(glyph[2])
        for ry in range(FONT_ROWS):
            row = glyph[ry] if ry < len(glyph) else ""
            for rx in range(gw):
                if rx < len(row) and row[rx] == "#":
                    grid[ry][cx + rx] = 2 if (c == "i" and ry == ACCENT_ROW) else 1
        cx += gw + 1
    return grid


_GRID = _pixel_grid()
WIDTH = len(_GRID[0])
HEIGHT = (FONT_ROWS + 1) // 2  # terminal rows (two pixel rows per cell)


def render(screen: Screen, x: int, y: int, body: RGB, accent: RGB,
           bg: RGB | None = None) -> None:
    """Draw the wordmark with its top-left cell at (x, y)."""
    for cy in range(HEIGHT):
        top_row = _GRID[cy * 2]
        bottom_row = _GRID[cy * 2 + 1] if cy * 2 + 1 < FONT_ROWS else [0] * WIDTH
        for cx in range(WIDTH):
            top, bottom = top_row[cx], bottom_row[cx]
            if not top and not bottom:
                continue
            # Pick the char for which pixels are lit; when both halves are lit
            # with different colors, the (rare) accent wins for the whole cell.
            color = accent if 2 in (top, bottom) else body
            if top and bottom:
                char = "█"
            elif top:
                char = "▀"
            else:
                char = "▄"
            screen.put(x + cx, y + cy, char, Style(fg=color, bg=bg))
