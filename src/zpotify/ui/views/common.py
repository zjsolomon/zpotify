"""Shared list plumbing for the track-based views."""

from __future__ import annotations

from typing import Callable

from zpotify.models import Track
from zpotify.term.events import Key, Mouse
from zpotify.term.screen import Screen
from zpotify.term.style import Style
from zpotify.term.widgets import ListView
from zpotify.ui import theme


def track_row(track: Track, selected: bool, width: int) -> list[tuple[str, Style]]:
    base = theme.ROW_SELECTED if selected else theme.ROW
    dim = theme.ROW_DIM_SELECTED if selected else theme.ROW_DIM
    time_col = _mmss(track.duration_ms)
    name_w = max(10, int(width * 0.45))
    artist_w = max(8, width - name_w - len(time_col) - 4)
    name = _pad(track.name, name_w)
    artist = _pad(track.artist, artist_w)
    return [(" " + name + " ", base), (artist + " ", dim), (time_col, dim)]


def make_track_list() -> ListView:
    return ListView(rows=[], render_row=track_row)


def list_nav(listview: ListView, key: Key) -> bool:
    """Standard j/k/arrow/page navigation. Returns True if consumed."""
    if key.name == "up" or key.char == "k":
        listview.move(-1)
    elif key.name == "down" or key.char == "j":
        listview.move(1)
    elif key.name == "pgup":
        listview.page(-1)
    elif key.name == "pgdn":
        listview.page(1)
    elif key.name == "home":
        listview.home()
    elif key.name == "end":
        listview.end()
    else:
        return False
    return True


def wire_list_mouse(app, listview: ListView, x: int, y: int, w: int, h: int,
                    on_activate: Callable[[int], None]) -> None:
    """Register click/scroll/double-click handling for a rendered list."""
    def handler(mouse: Mouse) -> None:
        if mouse.kind in ("scroll_up", "scroll_down"):
            listview.scroll(-3 if mouse.kind == "scroll_up" else 3)
        elif mouse.kind == "press" and mouse.button == 1:
            index = listview.click(mouse.y - y)
            if index is not None and getattr(listview, "_last_click", None) == index:
                on_activate(index)
            if index is not None:
                listview._last_click = index  # double-click-ish: second click plays
    app.add_hit(x, y, w, h, handler)


def _pad(text: str, width: int) -> str:
    text = text[:width]
    return text + " " * (width - len(text))


def _mmss(ms: int) -> str:
    seconds = max(0, ms // 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"
