"""Playlists: your playlists on the left, drill into tracks on enter."""

from __future__ import annotations

from zpotify.models import Playlist
from zpotify.term.events import Key
from zpotify.term.screen import Screen
from zpotify.term.style import Style
from zpotify.term.widgets import ListView
from zpotify.ui import theme
from zpotify.ui.views import common
from zpotify.ui.views.base import View


def _playlist_row(playlist: Playlist, selected: bool, width: int) -> list[tuple[str, Style]]:
    base = theme.ROW_SELECTED if selected else theme.ROW
    dim = theme.ROW_DIM_SELECTED if selected else theme.ROW_DIM
    count = f"{playlist.total_tracks:>4} ♪"
    name_w = max(10, width - len(count) - 3)
    name = playlist.name[:name_w]
    return [(" " + name + " " * (name_w - len(name)) + " ", base), (count, dim)]


class PlaylistsView(View):
    name = "playlists"

    def __init__(self) -> None:
        self.playlists = ListView(rows=[], render_row=_playlist_row)
        self.tracks = common.make_track_list()
        self.mode = "lists"  # lists | tracks
        self.current: Playlist | None = None
        self.loaded = False
        self.loading = False
        self.blocked = False  # Spotify refuses to list this playlist's tracks

    def on_show(self, app) -> None:
        if not self.loaded and not self.loading:
            self.reload(app)

    def reload(self, app) -> None:
        self.loading = True
        def done(rows):
            self.loading = False
            self.loaded = True
            self.playlists.rows = rows
        def failed(_exc):
            self.loading = False  # a failed load must stay retryable
        app.call_api(app.api.my_playlists, then=done, refresh=False,
                     describe="playlists", on_error=failed)

    def handle_key(self, app, key: Key) -> bool:
        active = self.playlists if self.mode == "lists" else self.tracks
        if common.list_nav(active, key):
            return True
        if key.name == "enter":
            if self.mode == "lists":
                self._open_selected(app)
            else:
                self._play_selected(app)
            return True
        if key.name in ("esc", "left") or key.char == "h":
            if self.mode == "tracks":
                self.mode = "lists"
                return True
        if key.char == "R":
            if self.mode == "lists":
                self.reload(app)
            elif self.current is not None:
                self._load_tracks(app)
            return True
        if key.char == "a" and self.mode == "tracks" and self.tracks.rows:
            track = self.tracks.rows[self.tracks.selected]
            app.call_api(lambda: app.api.add_to_queue(track.uri),
                         refresh=False, describe="queue")
            app.notify(f"queued: {track.name}")
            app.refresh_queue_soon()
            return True
        return False

    def _open_selected(self, app) -> None:
        if not self.playlists.rows:
            return
        self.current = self.playlists.rows[self.playlists.selected]
        self.mode = "tracks"
        self._load_tracks(app)

    def _load_tracks(self, app) -> None:
        playlist = self.current
        if playlist is None:
            return
        self.tracks.rows = []
        self.blocked = False
        def done(rows):
            self.tracks.rows = rows
            self.tracks.selected = 0
            self.tracks.offset = 0
        def failed(exc):
            from zpotify.api import ApiError
            if isinstance(exc, ApiError) and exc.status == 403:
                # Spotify blocks personal apps from listing playlists the
                # user doesn't own — but playing them as a context works.
                self.blocked = True
            else:
                self.mode = "lists"  # bounce back so the view isn't a dead end
        app.call_api(lambda: app.api.playlist_tracks(playlist.id), then=done,
                     refresh=False, describe="playlist", on_error=failed)

    def _play_selected(self, app) -> None:
        if self.current is None:
            return
        if self.blocked or not self.tracks.rows:
            # can't list the tracks, but Spotify will happily play the context
            app.play_tracks(context_uri=self.current.uri)
            return
        app.play_tracks(context_uri=self.current.uri,
                        offset_position=self.tracks.selected)

    def render(self, app, screen: Screen, x: int, y: int, w: int, h: int) -> None:
        if self.mode == "lists":
            header = "your playlists" + ("  (loading…)" if self.loading
                                         else "  (R refreshes)")
            screen.put(x + 2, y, header, theme.DIM)
            if self.playlists.rows:
                self.playlists.render(screen, x + 1, y + 1, w - 2, h - 1)
                common.wire_list_mouse(app, self.playlists, x + 1, y + 1, w - 2, h - 1,
                                       lambda i: self._open_selected(app))
            return
        title = self.current.name if self.current else ""
        screen.put(x + 2, y, f"◀ {title}", theme.ACCENT_BOLD)
        screen.put(x + 2 + len(title) + 4, y, "(esc to go back, enter plays)", theme.FAINT)
        back_w = len(title) + 4
        app.add_hit(x + 2, y, back_w, 1,
                    lambda m: m.kind == "press" and setattr(self, "mode", "lists"))
        if self.tracks.rows:
            self.tracks.render(screen, x + 1, y + 1, w - 2, h - 1)
            common.wire_list_mouse(app, self.tracks, x + 1, y + 1, w - 2, h - 1,
                                   lambda i: self._play_selected(app))
        elif self.blocked:
            screen.put(x + 2, y + 2,
                       "Spotify doesn't let personal API apps read playlists you",
                       theme.DIM)
            screen.put(x + 2, y + 3,
                       "don't own — but it can still play. Press enter to play it.",
                       theme.DIM)

            def play_click(mouse):
                if mouse.kind == "press":
                    self._play_selected(app)
            app.add_hit(x + 1, y + 2, w - 2, 2, play_click)
        else:
            screen.put(x + 2, y + 2, "loading…", theme.FAINT)
