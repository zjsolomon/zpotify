"""Search view: text input + result lists (tracks, albums, playlists)."""

from __future__ import annotations

from zpotify.models import SearchResults
from zpotify.term.events import Key
from zpotify.term.screen import Screen
from zpotify.term.widgets import ListView, TextInput
from zpotify.ui import theme
from zpotify.ui.views import common
from zpotify.ui.views.base import View


class SearchView(View):
    name = "search"

    def __init__(self) -> None:
        self.query = TextInput()
        self.focused = False
        self.results: SearchResults | None = None
        self.tracks = common.make_track_list()
        self.searching = False

    def focus_input(self) -> None:
        self.focused = True

    @property
    def wants_text(self) -> bool:
        return self.focused

    def handle_key(self, app, key: Key) -> bool:
        if self.focused:
            if key.name == "enter":
                self._search(app)
                return True
            if key.name == "esc":
                self.focused = False
                return True
            if key.name in ("down", "tab"):
                self.focused = False
                return True
            return self.query.handle_key(key)
        if key.name == "tab":
            self.focused = True
            return True
        if common.list_nav(self.tracks, key):
            return True
        if key.name == "enter":
            if not self.tracks.rows:
                self.focused = True
                return True
            self._play_selected(app)
            return True
        if key.char == "a":
            self._queue_selected(app)
            return True
        return False

    def _search(self, app) -> None:
        text = self.query.value.strip()
        if not text:
            return
        self.searching = True
        def done(results):
            self.searching = False
            self.results = results
            self.tracks.rows = results.tracks
            self.tracks.selected = 0
            self.tracks.offset = 0
            if results.tracks:
                self.focused = False
        def failed(_exc):
            self.searching = False  # stay retryable
        app.call_api(lambda: app.api.search(text), then=done,
                     refresh=False, describe="search", on_error=failed)

    def _play_selected(self, app) -> None:
        rows = self.tracks.rows
        if not rows:
            return
        index = self.tracks.selected
        uris = [t.uri for t in rows[index:index + 50]]
        app.play_tracks(uris=uris)

    def _queue_selected(self, app) -> None:
        rows = self.tracks.rows
        if not rows:
            return
        track = rows[self.tracks.selected]
        app.call_api(lambda: app.api.add_to_queue(track.uri),
                     refresh=False, describe="queue")
        app.notify(f"queued: {track.name}")
        app.refresh_queue_soon()

    def render(self, app, screen: Screen, x: int, y: int, w: int, h: int) -> None:
        screen.put(x + 2, y + 1, "search:", theme.DIM)
        input_x = x + 10
        input_w = min(60, w - input_x - 2)
        self.query.render(screen, input_x, y + 1, input_w,
                          theme.INPUT_FOCUS if self.focused else theme.INPUT,
                          self.focused)
        app.add_hit(input_x, y + 1, input_w, 1,
                    lambda m: m.kind == "press" and self.focus_input())
        if self.searching:
            status = "searching…"
        elif self.results:
            status = f"{len(self.tracks.rows)} tracks — enter plays, a queues"
        elif not self.focused:
            status = "enter to type a query"
        else:
            status = "type a query, enter to search"
        screen.put(x + 2, y + 2, status, theme.FAINT)

        list_y = y + 4
        list_h = h - 5
        if list_h > 0 and self.tracks.rows:
            self.tracks.render(screen, x + 1, list_y, w - 2, list_h)
            common.wire_list_mouse(app, self.tracks, x + 1, list_y, w - 2, list_h,
                                   lambda i: self._play_selected(app))
