"""Library: your saved (liked) tracks."""

from __future__ import annotations

from zpotify.term.events import Key
from zpotify.term.screen import Screen
from zpotify.ui import theme
from zpotify.ui.views import common
from zpotify.ui.views.base import View


class LibraryView(View):
    name = "library"

    def __init__(self) -> None:
        self.tracks = common.make_track_list()
        self.loaded = False
        self.loading = False

    def on_show(self, app) -> None:
        if not self.loaded and not self.loading:
            self.reload(app)

    def reload(self, app) -> None:
        self.loading = True
        def done(rows):
            self.loading = False
            self.loaded = True
            self.tracks.rows = rows
        app.call_api(app.api.saved_tracks, then=done, refresh=False,
                     describe="library")

    def handle_key(self, app, key: Key) -> bool:
        if common.list_nav(self.tracks, key):
            return True
        if not self.tracks.rows:
            return False
        track = self.tracks.rows[self.tracks.selected]
        if key.name == "enter":
            self._play_selected(app)
            return True
        if key.char == "a":
            app.call_api(lambda: app.api.add_to_queue(track.uri),
                         refresh=False, describe="queue")
            app.notify(f"queued: {track.name}")
            return True
        if key.char == "f":
            index = self.tracks.selected
            app.call_api(lambda: app.api.unsave_track(track.id), refresh=False,
                         then=lambda _: self._remove_row(index),
                         describe="unsave")
            app.notify(f"removed from library: {track.name}")
            return True
        return False

    def _remove_row(self, index: int) -> None:
        if 0 <= index < len(self.tracks.rows):
            del self.tracks.rows[index]
            self.tracks.selected = min(self.tracks.selected,
                                       max(0, len(self.tracks.rows) - 1))

    def _play_selected(self, app) -> None:
        rows = self.tracks.rows
        if not rows:
            return
        index = self.tracks.selected
        uris = [t.uri for t in rows[index:index + 50]]
        app.play_tracks(uris=uris)

    def render(self, app, screen: Screen, x: int, y: int, w: int, h: int) -> None:
        header = "liked songs" + ("  (loading…)" if self.loading
                                  else f"  ({len(self.tracks.rows)})")
        screen.put(x + 2, y, header, theme.DIM)
        screen.put(x + w - 30, y, "enter plays · a queues · f unsaves"[:28], theme.FAINT)
        if self.tracks.rows:
            self.tracks.render(screen, x + 1, y + 1, w - 2, h - 1)
            common.wire_list_mouse(app, self.tracks, x + 1, y + 1, w - 2, h - 1,
                                   lambda i: self._play_selected(app))
