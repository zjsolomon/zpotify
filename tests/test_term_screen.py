"""Frame-buffer and diff-renderer tests (no tty; StringIO out + injected size)."""

from __future__ import annotations

import io

from zpotify.term.screen import Screen
from zpotify.term.style import DEFAULT, Style


def make(cols: int = 80, rows: int = 24) -> tuple[Screen, io.StringIO]:
    out = io.StringIO()
    return Screen(out=out, size=(cols, rows)), out


def cell(screen: Screen, x: int, y: int):
    return screen._cur[y][x]


def test_size_injection() -> None:
    screen, _ = make(40, 10)
    assert screen.size == (40, 10)


def test_put_basic() -> None:
    screen, _ = make()
    screen.put(2, 1, "hi")
    assert cell(screen, 2, 1)[0] == "h"
    assert cell(screen, 3, 1)[0] == "i"


def test_put_clips_horizontally() -> None:
    screen, _ = make(5, 3)
    screen.put(3, 0, "abcdef")  # only cols 3,4 fit
    assert cell(screen, 3, 0)[0] == "a"
    assert cell(screen, 4, 0)[0] == "b"


def test_put_ignores_control_chars() -> None:
    screen, _ = make()
    screen.put(0, 0, "a\x07b")  # bell dropped, chars stay adjacent
    assert cell(screen, 0, 0)[0] == "a"
    assert cell(screen, 1, 0)[0] == "b"


def test_put_out_of_bounds_row() -> None:
    screen, _ = make(10, 2)
    screen.put(0, 5, "x")  # no crash, nothing drawn


def test_wide_char_takes_two_cells() -> None:
    screen, _ = make()
    screen.put(0, 0, "世")
    assert cell(screen, 0, 0)[0] == "世"
    assert cell(screen, 1, 0)[0] == "\x00"  # continuation marker


def test_wide_char_at_last_column_pads() -> None:
    screen, _ = make(3, 1)
    screen.put(2, 0, "世")  # no room for the trailing cell
    assert cell(screen, 2, 0)[0] == " "


def test_diff_emits_nothing_when_unchanged() -> None:
    screen, out = make()
    screen.clear()
    screen.present()  # first present paints the blank frame
    out.truncate(0)
    out.seek(0)
    screen.present()  # nothing changed since
    assert out.getvalue() == ""


def test_diff_minimal_on_single_cell_change() -> None:
    screen, out = make()
    screen.clear()
    screen.present()
    out.truncate(0)
    out.seek(0)
    screen.put(5, 2, "X")
    screen.present()
    output = out.getvalue()
    assert "X" in output
    # One cursor move to (row 3, col 6), 1-based.
    assert output.count("\x1b[3;6H") == 1
    # Only the single dirty cell is addressed.
    assert output.count("H") == 1


def test_diff_style_change_reemits_sgr() -> None:
    screen, out = make()
    screen.clear()
    screen.present()
    out.truncate(0)
    out.seek(0)
    red = Style(fg=(255, 0, 0))
    screen.put(0, 0, "A", red)
    screen.present()
    assert red.sgr() in out.getvalue()


def test_box_draws_rounded_corners() -> None:
    screen, _ = make()
    screen.box(0, 0, 4, 3)
    assert cell(screen, 0, 0)[0] == "╭"
    assert cell(screen, 3, 0)[0] == "╮"
    assert cell(screen, 0, 2)[0] == "╰"
    assert cell(screen, 3, 2)[0] == "╯"


def test_box_title() -> None:
    screen, _ = make()
    screen.box(0, 0, 10, 3, title="Hi")
    row = "".join(cell(screen, x, 0)[0] for x in range(10))
    assert "Hi" in row


def test_clear_applies_style() -> None:
    screen, _ = make(4, 1)
    blue = Style(bg=(0, 0, 255))
    screen.clear(blue)
    assert cell(screen, 0, 0) == (" ", blue)


def test_fill() -> None:
    screen, _ = make(10, 5)
    screen.fill(1, 1, 3, 2, "#")
    assert cell(screen, 1, 1)[0] == "#"
    assert cell(screen, 3, 2)[0] == "#"
    assert cell(screen, 4, 2)[0] == " "
