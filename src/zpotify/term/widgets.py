"""Stateless-ish render helpers the UI composes onto a :class:`Screen`.

Each widget renders into a caller-supplied rectangle and translates mouse
coordinates back to logical positions. None of them own the event loop.
"""

from __future__ import annotations

from typing import Callable

from .events import Key
from .screen import Screen
from .style import DEFAULT, Style

# Segment list returned by a ListView row renderer.
Segment = tuple[str, Style]
RenderRow = Callable[[object, bool, int], list[Segment]]

# Left-edge partial blocks by eighths (1/8 .. 7/8); index 0 unused.
_EIGHTHS = ["", "▏", "▎", "▍", "▌", "▋", "▊", "▉"]


def _clamp(value: int, lo: int, hi: int) -> int:
    return lo if value < lo else hi if value > hi else value


def _draw_segments(screen: Screen, x: int, y: int, width: int,
                   segments: list[Segment], base: Style) -> None:
    """Draw ``segments`` left to right, padding/clipping to ``width``."""
    cx = x
    end = x + width
    for text, style in segments:
        if cx >= end:
            break
        chunk = text[: end - cx]
        screen.put(cx, y, chunk, style)
        cx += len(chunk)
    if cx < end:
        screen.put(cx, y, " " * (end - cx), base)


class ListView:
    """Generic scrollable, selectable list with an optional scrollbar."""

    def __init__(self, rows: list[object], render_row: RenderRow,
                 selected: int = 0, offset: int = 0) -> None:
        self.rows = rows
        self.render_row = render_row
        self.selected = selected
        self.offset = offset
        self._height = 0
        self._width = 0

    def move(self, delta: int) -> None:
        """Move the selection by ``delta`` rows and keep it visible."""
        if not self.rows:
            return
        self.selected = _clamp(self.selected + delta, 0, len(self.rows) - 1)
        self.ensure_visible()

    def page(self, delta: int) -> None:
        """Move the selection by ``delta`` pages."""
        self.move(delta * max(1, self._height))

    def home(self) -> None:
        self.selected = 0
        self.ensure_visible()

    def end(self) -> None:
        self.selected = max(0, len(self.rows) - 1)
        self.ensure_visible()

    def ensure_visible(self) -> None:
        """Scroll ``offset`` the minimum amount to reveal the selection."""
        h = self._height
        if h <= 0:
            return
        if self.selected < self.offset:
            self.offset = self.selected
        elif self.selected >= self.offset + h:
            self.offset = self.selected - h + 1
        self._clamp_offset()

    def scroll(self, delta: int) -> None:
        """Scroll the view by ``delta`` rows without moving the selection."""
        self.offset += delta
        self._clamp_offset()

    def click(self, rel_y: int) -> int | None:
        """Map a click at row ``rel_y`` to a row index, selecting it."""
        idx = self.offset + rel_y
        if 0 <= idx < len(self.rows):
            self.selected = idx
            return idx
        return None

    def _clamp_offset(self) -> None:
        max_off = max(0, len(self.rows) - max(1, self._height))
        self.offset = _clamp(self.offset, 0, max_off)

    def render(self, screen: Screen, x: int, y: int, w: int, h: int,
               base: Style = DEFAULT) -> None:
        """Render into the ``w``×``h`` rect at ``(x, y)``."""
        self._height = h
        self._width = w
        self._clamp_offset()
        overflow = len(self.rows) > h
        content_w = w - 1 if overflow else w
        for i in range(h):
            row_idx = self.offset + i
            if row_idx < len(self.rows):
                segs = self.render_row(
                    self.rows[row_idx], row_idx == self.selected, content_w)
                _draw_segments(screen, x, y + i, content_w, segs, base)
            else:
                screen.put(x, y + i, " " * content_w, base)
        if overflow:
            self._render_scrollbar(screen, x + w - 1, y, h)

    def _render_scrollbar(self, screen: Screen, x: int, y: int, h: int) -> None:
        n = len(self.rows)
        thumb = max(1, h * h // n)
        max_off = max(1, n - h)
        pos = _clamp((self.offset * (h - thumb)) // max_off, 0, h - thumb)
        for i in range(h):
            ch = "▐" if pos <= i < pos + thumb else "░"
            screen.put(x, y + i, ch, DEFAULT)


class ProgressBar:
    """A fractional bar with eighth-block sub-cell resolution."""

    @staticmethod
    def render(screen: Screen, x: int, y: int, w: int, fraction: float,
               style_done: Style, style_todo: Style) -> None:
        """Render a bar of ``w`` cells filled to ``fraction`` (0..1)."""
        if w <= 0:
            return
        fraction = 0.0 if fraction < 0 else 1.0 if fraction > 1 else fraction
        total_eighths = round(fraction * w * 8)
        full = total_eighths // 8
        rem = total_eighths % 8
        cx = x
        if full:
            screen.put(cx, y, "█" * full, style_done)
            cx += full
        if cx < x + w and rem:
            screen.put(cx, y, _EIGHTHS[rem], style_done)
            cx += 1
        if cx < x + w:
            screen.put(cx, y, "░" * (x + w - cx), style_todo)

    @staticmethod
    def hit(rel_x: int, w: int) -> float:
        """Map a click at column ``rel_x`` to a fraction (0..1)."""
        if w <= 1:
            return 0.0
        f = rel_x / (w - 1)
        return 0.0 if f < 0 else 1.0 if f > 1 else f


class TextInput:
    """Single-line editable text field with horizontal scroll."""

    def __init__(self, value: str = "", cursor: int | None = None) -> None:
        self.value = value
        self.cursor = len(value) if cursor is None else cursor
        self._scroll = 0

    def handle_key(self, key: Key) -> bool:
        """Apply an edit/navigation key. Returns True if it was consumed."""
        if key.ctrl or key.alt:
            return False
        if key.name == "space":
            return self._insert(" ")
        if key.char and not key.name:
            return self._insert(key.char)
        if key.name == "backspace":
            if self.cursor > 0:
                self.value = self.value[:self.cursor - 1] + self.value[self.cursor:]
                self.cursor -= 1
                return True
            return False
        if key.name == "delete":
            if self.cursor < len(self.value):
                self.value = self.value[:self.cursor] + self.value[self.cursor + 1:]
                return True
            return False
        if key.name == "left":
            self.cursor = max(0, self.cursor - 1)
            return True
        if key.name == "right":
            self.cursor = min(len(self.value), self.cursor + 1)
            return True
        if key.name == "home":
            self.cursor = 0
            return True
        if key.name == "end":
            self.cursor = len(self.value)
            return True
        return False

    def _insert(self, text: str) -> bool:
        self.value = self.value[:self.cursor] + text + self.value[self.cursor:]
        self.cursor += len(text)
        return True

    def render(self, screen: Screen, x: int, y: int, w: int,
               style: Style = DEFAULT, focus: bool = False) -> None:
        """Render the field, scrolling so the cursor stays visible."""
        if w <= 0:
            return
        # Keep the cursor within the visible window.
        if self.cursor < self._scroll:
            self._scroll = self.cursor
        elif self.cursor >= self._scroll + w:
            self._scroll = self.cursor - w + 1
        visible = self.value[self._scroll:self._scroll + w]
        screen.put(x, y, visible.ljust(w), style)
        if focus:
            cx = x + (self.cursor - self._scroll)
            if x <= cx < x + w:
                under = self.value[self.cursor] if self.cursor < len(self.value) else " "
                screen.put(cx, y, under, style.with_(reverse=not style.reverse))


def tabs(screen: Screen, x: int, y: int, labels: list[str], active_index: int,
         style_active: Style, style_inactive: Style) -> list[tuple[int, int]]:
    """Draw a row of tabs; return each label's ``(x_start, x_end)`` hit range."""
    ranges: list[tuple[int, int]] = []
    cx = x
    for i, label in enumerate(labels):
        text = f" {label} "
        style = style_active if i == active_index else style_inactive
        screen.put(cx, y, text, style)
        ranges.append((cx, cx + len(text)))
        cx += len(text)
    return ranges
