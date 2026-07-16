"""Tests for the 8-bit zpotify wordmark."""

from __future__ import annotations

import io

from zpotify.term.screen import Screen
from zpotify.ui import wordmark


def test_grid_shape_and_accent() -> None:
    grid = wordmark._pixel_grid()
    assert len(grid) == wordmark.FONT_ROWS
    assert all(len(row) == wordmark.WIDTH for row in grid)
    accents = [(y, x) for y, row in enumerate(grid) for x, v in enumerate(row) if v == 2]
    assert accents and all(y == wordmark.ACCENT_ROW for y, _ in accents)  # only the i dot
    lit = sum(v != 0 for row in grid for v in row)
    assert lit > 60  # sanity: the word is actually drawn


def test_render_draws_blocks_within_bounds() -> None:
    out = io.StringIO()
    screen = Screen(out=out, size=(60, 10))
    wordmark.render(screen, 2, 1, body=(255, 255, 255), accent=(30, 215, 96))
    screen.present()
    text = out.getvalue()
    assert any(ch in text for ch in "▀▄█")
    # right-edge clipping must not raise
    wordmark.render(screen, 55, 1, body=(255, 255, 255), accent=(30, 215, 96))
    screen.present()
