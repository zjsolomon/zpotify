"""Interactive self-test for the terminal engine.

Run with ``uv run python -m zpotify.term.demo``. Exercises the frame buffer,
diff renderer, input decoder, mouse/paste reporting and every widget. Press
``q`` or Ctrl+C to exit. There are no assertions here — a human drives it.
"""

from __future__ import annotations

import selectors

from .events import Key, Mouse, Paste, Resize
from .input import InputReader
from .screen import Screen
from .style import Style
from .widgets import ListView, ProgressBar, TextInput, tabs

_ESC_TIMEOUT = 0.025  # seconds to wait before treating a lone ESC as Esc
_TICK = 1 / 30        # ~30 fps

_BG = Style(bg=(24, 24, 32))
_FRAME = Style(fg=(120, 200, 140), bg=(24, 24, 32))
_TEXT = Style(fg=(220, 220, 220), bg=(24, 24, 32))
_DIM = Style(fg=(120, 120, 130), bg=(24, 24, 32))
_SEL = Style(fg=(20, 20, 20), bg=(120, 200, 140))
_TAB_ON = Style(fg=(20, 20, 20), bg=(120, 200, 140), bold=True)
_TAB_OFF = Style(fg=(180, 180, 180), bg=(48, 48, 60))
_DONE = Style(fg=(120, 200, 140), bg=(24, 24, 32))
_TODO = Style(fg=(70, 70, 80), bg=(24, 24, 32))


def _row_renderer(row: object, selected: bool, width: int) -> list[tuple[str, Style]]:
    style = _SEL if selected else _TEXT
    marker = "▶ " if selected else "  "
    text = f"{marker}{row}".ljust(width)[:width]
    return [(text, style)]


class Demo:
    """Holds all demo state and draws one frame per tick."""

    def __init__(self) -> None:
        self.screen = Screen()
        self.reader = InputReader()
        self.tab_labels = ["Home", "Search", "Library", "Now Playing"]
        self.active_tab = 0
        self.tab_hits: list[tuple[int, int]] = []
        self.list = ListView(
            [f"Track {i:03d} — Artist {i % 17}" for i in range(100)],
            _row_renderer,
        )
        self.text = TextInput("type here…")
        self.fraction = 0.35
        self.last_event = "(none)"
        self.progress_rect = (0, 0, 0)  # x, y, w of the progress bar
        self.running = True

    # -- event handling --------------------------------------------------

    def dispatch(self, event: object) -> None:
        self.last_event = str(event)
        if isinstance(event, Key):
            self._on_key(event)
        elif isinstance(event, Mouse):
            self._on_mouse(event)
        elif isinstance(event, Resize):
            self.screen.resize()
        elif isinstance(event, Paste):
            for ch in event.text:
                self.text.handle_key(Key(char=ch))

    def _on_key(self, key: Key) -> None:
        if key.char == "q" or (key.ctrl and key.char == "c"):
            self.running = False
            return
        if key.name == "up":
            self.list.move(-1)
        elif key.name == "down":
            self.list.move(1)
        elif key.name == "pgup":
            self.list.page(-1)
        elif key.name == "pgdn":
            self.list.page(1)
        elif key.name == "home":
            self.list.home()
        elif key.name == "end":
            self.list.end()
        elif key.name == "tab":
            self.active_tab = (self.active_tab + 1) % len(self.tab_labels)
        elif key.name == "left":
            self.fraction = max(0.0, self.fraction - 0.02)
        elif key.name == "right":
            self.fraction = min(1.0, self.fraction + 0.02)
        else:
            self.text.handle_key(key)

    def _on_mouse(self, m: Mouse) -> None:
        if m.kind == "scroll_up":
            self.list.scroll(-3)
            return
        if m.kind == "scroll_down":
            self.list.scroll(3)
            return
        if m.kind != "press":
            return
        for i, (start, end) in enumerate(self.tab_hits):
            if start <= m.x < end and m.y == 2:
                self.active_tab = i
                return
        lx, ly, lw, lh = self._list_rect()
        if lx <= m.x < lx + lw and ly <= m.y < ly + lh:
            self.list.click(m.y - ly)
            return
        px, py, pw = self.progress_rect
        if py == m.y and px <= m.x < px + pw:
            self.fraction = ProgressBar.hit(m.x - px, pw)

    # -- layout ----------------------------------------------------------

    def _list_rect(self) -> tuple[int, int, int, int]:
        cols, rows = self.screen.size
        return 2, 5, cols - 4, rows - 11

    # -- rendering -------------------------------------------------------

    def render(self) -> None:
        screen = self.screen
        cols, rows = screen.size
        screen.clear(_BG)
        screen.box(0, 0, cols, rows, _FRAME, title="zpotify terminal engine")
        self.tab_hits = tabs(screen, 2, 2, self.tab_labels, self.active_tab,
                             _TAB_ON, _TAB_OFF)
        lx, ly, lw, lh = self._list_rect()
        if lh > 0:
            self.list.render(screen, lx, ly, lw, lh, _BG)

        self.text.render(screen, 2, rows - 5, cols - 4, _TEXT, focus=True)

        px, py, pw = 2, rows - 3, cols - 4
        self.progress_rect = (px, py, pw)
        ProgressBar.render(screen, px, py, pw, self.fraction, _DONE, _TODO)

        status = (f"size {cols}x{rows}   sel {self.list.selected}   "
                  f"last: {self.last_event}")
        screen.put(2, rows - 2, status.ljust(cols - 4)[: cols - 4], _DIM)
        screen.put(2, rows - 8, "q or Ctrl+C to quit", _DIM)
        screen.present()

    # -- loop ------------------------------------------------------------

    def run(self) -> None:
        sel = selectors.DefaultSelector()
        with self.screen:
            self.reader.install_resize_handler()
            sel.register(self.reader, selectors.EVENT_READ)
            self.render()
            while self.running:
                ready = sel.select(timeout=_TICK)
                if ready:
                    for event in self.reader.read():
                        self.dispatch(event)
                elif self.reader.pending_escape:
                    # No bytes followed the lone ESC within the tick window.
                    for event in self.reader.flush_escape():
                        self.dispatch(event)
                self.render()


def main() -> None:
    try:
        Demo().run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
