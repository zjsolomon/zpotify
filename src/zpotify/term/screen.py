"""Raw-mode terminal surface with a diffing cell frame buffer.

No curses: alternate screen, mouse/paste reporting and styled cells are all
driven through raw ANSI escape codes over a termios raw tty. ``Screen`` owns a
current and previous frame; :meth:`present` emits the minimal byte sequence that
turns the previous frame into the current one and performs a single flush.
"""

from __future__ import annotations

import atexit
import os
import sys
import unicodedata
from typing import IO

from .style import DEFAULT, Style

# Sentinel char occupying the second cell of a wide (double-width) glyph. The
# differ prints the wide char once and skips this marker.
_CONT = "\x00"

# Cell = (char, Style). A fresh previous frame is filled with this sentinel so
# the first present() after enter()/resize() repaints everything.
Cell = tuple[str, Style]
_NULL_CELL: Cell = ("￿", DEFAULT)


def _terminal_size() -> tuple[int, int]:
    """Terminal size, falling back to 80x24 when stdout isn't a tty."""
    try:
        return tuple(os.get_terminal_size())
    except OSError:
        return (80, 24)


def _char_width(ch: str) -> int:
    """Columns a character occupies: 2 for East-Asian wide/fullwidth, else 1."""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


class Screen:
    """A styled, diff-rendered terminal surface."""

    def __init__(self, out: IO[str] | None = None,
                 size: tuple[int, int] | None = None) -> None:
        self._out = out if out is not None else sys.stdout
        self._fixed_size = size
        cols, rows = size if size is not None else _terminal_size()
        self.cols = cols
        self.rows = rows
        self._active = False
        self._old_termios = None
        self._fd = -1
        self._alloc()

    # -- buffers ---------------------------------------------------------

    def _alloc(self) -> None:
        self._cur: list[list[Cell]] = [
            [(" ", DEFAULT) for _ in range(self.cols)] for _ in range(self.rows)
        ]
        self._prev: list[list[Cell]] = [
            [_NULL_CELL for _ in range(self.cols)] for _ in range(self.rows)
        ]

    @property
    def size(self) -> tuple[int, int]:
        """Current ``(cols, rows)``."""
        return (self.cols, self.rows)

    def resize(self) -> tuple[int, int]:
        """Re-read the terminal size, reallocate buffers, force a full repaint."""
        if self._fixed_size is not None:
            self.cols, self.rows = self._fixed_size
        else:
            self.cols, self.rows = _terminal_size()
        self._alloc()
        return self.size

    # -- lifecycle -------------------------------------------------------

    def enter(self) -> "Screen":
        """Enter raw mode, alternate screen and enable mouse/paste reporting."""
        if self._active:
            return self
        import termios
        import tty

        self._fd = sys.stdin.fileno()
        self._old_termios = termios.tcgetattr(self._fd)
        tty.setraw(self._fd)
        # Non-blocking reads: the input layer polls via selectors.
        mode = termios.tcgetattr(self._fd)
        mode[6][termios.VMIN] = 0
        mode[6][termios.VTIME] = 0
        termios.tcsetattr(self._fd, termios.TCSANOW, mode)

        self._write(
            "\x1b[?1049h"   # alternate screen buffer
            "\x1b[?25l"     # hide cursor
            "\x1b[?1002h"   # button-event mouse tracking (press/release/drag)
            "\x1b[?1006h"   # SGR extended mouse coordinates
            "\x1b[?2004h"   # bracketed paste
        )
        self._flush()
        self._active = True
        atexit.register(self.exit)
        return self

    def exit(self) -> None:
        """Restore the terminal. Safe to call twice / from atexit."""
        if not self._active:
            return
        self._active = False
        self._write(
            "\x1b[?2004l"
            "\x1b[?1006l"
            "\x1b[?1002l"
            "\x1b[?25h"
            "\x1b[?1049l"
        )
        self._flush()
        if self._old_termios is not None and self._fd >= 0:
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_termios)
            self._old_termios = None

    def __enter__(self) -> "Screen":
        return self.enter()

    def __exit__(self, *exc: object) -> None:
        self.exit()

    # -- drawing ---------------------------------------------------------

    def clear(self, style: Style = DEFAULT) -> None:
        """Fill the current frame with spaces in ``style``."""
        for row in self._cur:
            for x in range(self.cols):
                row[x] = (" ", style)

    def put(self, x: int, y: int, text: str, style: Style = DEFAULT) -> None:
        """Draw ``text`` at ``(x, y)``, clipping to bounds. Wide chars take two
        cells; control chars are ignored."""
        if not (0 <= y < self.rows):
            return
        row = self._cur[y]
        for ch in text:
            if x >= self.cols:
                break
            code = ord(ch)
            if code < 0x20 or code == 0x7f or unicodedata.category(ch) == "Cc":
                continue
            w = _char_width(ch)
            if w == 2:
                if x + 1 >= self.cols:
                    # No room for the trailing cell: pad rather than overflow.
                    if x >= 0:
                        row[x] = (" ", style)
                    x += 1
                    continue
                if x >= 0:
                    row[x] = (ch, style)
                    row[x + 1] = (_CONT, style)
                x += 2
            else:
                if x >= 0:
                    row[x] = (ch, style)
                x += 1

    def fill(self, x: int, y: int, w: int, h: int, char: str = " ",
             style: Style = DEFAULT) -> None:
        """Fill a ``w``×``h`` rectangle with ``char``."""
        for row in range(y, y + h):
            self.put(x, row, char * w, style)

    def hline(self, x: int, y: int, w: int, char: str = "─",
              style: Style = DEFAULT) -> None:
        """Draw a horizontal line of ``w`` cells."""
        self.put(x, y, char * w, style)

    def box(self, x: int, y: int, w: int, h: int, style: Style = DEFAULT,
            title: str | None = None) -> None:
        """Draw a rounded box; optional ``title`` sits in the top border."""
        if w < 2 or h < 2:
            return
        top = "╭" + "─" * (w - 2) + "╮"
        bottom = "╰" + "─" * (w - 2) + "╯"
        self.put(x, y, top, style)
        self.put(x, y + h - 1, bottom, style)
        for row in range(y + 1, y + h - 1):
            self.put(x, row, "│", style)
            self.put(x + w - 1, row, "│", style)
        if title:
            label = f" {title} "
            if len(label) > w - 2:
                label = label[: max(0, w - 2)]
            self.put(x + 1, y, label, style)

    # -- diff renderer ---------------------------------------------------

    def present(self) -> None:
        """Emit the minimal diff from the previous to the current frame."""
        out: list[str] = []
        pen: Style | None = None
        for y in range(self.rows):
            cur = self._cur[y]
            prev = self._prev[y]
            x = 0
            while x < self.cols:
                if cur[x] == prev[x]:
                    x += 1
                    continue
                # Start of a dirty run; walk to its end.
                run_start = x
                while x < self.cols and cur[x] != prev[x]:
                    x += 1
                run_end = x
                pen = self._emit_run(out, y, run_start, run_end, cur, pen)
        if out:
            self._write("".join(out))
            self._flush()
        # Swap: previous becomes a snapshot of what we just rendered.
        self._prev = [row[:] for row in self._cur]

    def _emit_run(self, out: list[str], y: int, start: int, end: int,
                  cur: list[Cell], pen: Style | None) -> Style | None:
        """Render cells [start, end) of row ``y``; return the trailing style."""
        cursor: int | None = None
        i = start
        while i < end:
            ch, style = cur[i]
            if ch == _CONT:
                # Continuation of a wide glyph already emitted (or a stray
                # marker); leave it to the preceding cell's advance.
                i += 1
                cursor = None
                continue
            if cursor != i:
                out.append(f"\x1b[{y + 1};{i + 1}H")
                cursor = i
            if style != pen:
                out.append(style.sgr())
                pen = style
            out.append(ch)
            w = _char_width(ch)
            cursor += w
            i += w
        return pen

    # -- misc ------------------------------------------------------------

    def set_title(self, title: str) -> None:
        """Set the terminal window title via OSC 2."""
        self._write(f"\x1b]2;{title}\x07")
        self._flush()

    def _write(self, s: str) -> None:
        self._out.write(s)

    def _flush(self) -> None:
        self._out.flush()
