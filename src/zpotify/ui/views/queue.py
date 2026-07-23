"""Queue: what Spotify will play next. Read-mostly; refreshes on show."""

from __future__ import annotations

from zpotify.term.events import Key
from zpotify.term.screen import Screen
from zpotify.ui import theme
from zpotify.ui.views import common
from zpotify.ui.views.base import View


class QueueView(View):
    name = "queue"

    def __init__(self) -> None:
        self.tracks = common.make_track_list()
        self.loading = False

    def on_show(self, app) -> None:
        self.reload(app)

    def reload(self, app) -> None:
        self.loading = True
        def done(rows):
            self.loading = False
            self.tracks.rows = rows
        def failed(_exc):
            self.loading = False  # stay retryable
        app.call_api(app.api.queue, then=done, refresh=False, describe="queue",
                     on_error=failed)

    def handle_key(self, app, key: Key) -> bool:
        if common.list_nav(self.tracks, key):
            return True
        if key.char == "R":
            self.reload(app)
            return True
        return False

    def render(self, app, screen: Screen, x: int, y: int, w: int, h: int) -> None:
        station = getattr(app, "station", None)
        label = "up next"
        if getattr(app, "up_next_is_radio", False) and station is not None:
            label = f"up next · radio — {station.label}"
        header = label + ("  (loading…)" if self.loading else "  (R refreshes)")
        screen.put(x + 2, y, header, theme.DIM)
        if self.tracks.rows:
            self.tracks.render(screen, x + 1, y + 1, w - 2, h - 1)
            common.wire_list_mouse(app, self.tracks, x + 1, y + 1, w - 2, h - 1,
                                   lambda i: None)
        elif not self.loading:
            screen.put(x + 2, y + 2, "queue is empty", theme.FAINT)
