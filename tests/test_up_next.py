"""Tests for the UP NEXT selection on the now-playing view."""

from __future__ import annotations

from zpotify.models import Track
from zpotify.term.events import Key
from zpotify.ui.views.now_playing import NowPlayingView


class StubApp:
    def __init__(self, n_tracks: int) -> None:
        self.up_next = [Track(f"t{i}", f"u{i}", f"Song{i}", ("A",), "Al", 1000)
                        for i in range(n_tracks)]
        self.skipped_to: int | None = None

    def skip_to_queue_index(self, index: int) -> None:
        self.skipped_to = index


def test_arrows_select_and_enter_skips() -> None:
    app = StubApp(5)
    view = NowPlayingView()
    assert view.handle_key(app, Key(name="down"))
    assert view.selected == 0
    view.handle_key(app, Key(name="down"))
    view.handle_key(app, Key(char="j"))
    assert view.selected == 2  # song D in the user's scenario (B=0, C=1, D=2)
    view.handle_key(app, Key(name="enter"))
    assert app.skipped_to == 2
    assert view.selected is None  # selection clears after playing


def test_up_past_top_clears_selection() -> None:
    app = StubApp(3)
    view = NowPlayingView()
    view.handle_key(app, Key(name="down"))
    view.handle_key(app, Key(name="up"))
    assert view.selected is None
    # up with no selection does not underflow or skip anything
    view.handle_key(app, Key(name="up"))
    assert view.selected is None and app.skipped_to is None


def test_selection_clamps_and_esc_clears() -> None:
    app = StubApp(2)
    view = NowPlayingView()
    for _ in range(9):
        view.handle_key(app, Key(name="down"))
    assert view.selected == 1  # clamped to the last row
    assert view.handle_key(app, Key(name="esc"))
    assert view.selected is None


def test_no_queue_means_keys_pass_through() -> None:
    app = StubApp(0)
    view = NowPlayingView()
    assert not view.handle_key(app, Key(name="down"))
    assert not view.handle_key(app, Key(name="enter"))
    assert app.skipped_to is None


def test_enter_plays_chain_from_selected_row_live() -> None:
    """Enter behaves like search: play the row directly, rest chained after."""
    from zpotify.models import Track as T

    class ChainApp(StubApp):
        def __init__(self, n):
            super().__init__(n)
            self.chained = None
            self._staged = True  # must be cleared regardless of mode

        def skip_to_queue_index(self, index):  # not used here
            raise AssertionError

    # exercise the real App method against a stub carrier
    from zpotify.ui.app import App
    app = ChainApp(6)
    app.up_next = [T(f"t{i}", f"spotify:track:{'x'*18}{i:04d}", f"S{i}", ("A",), "Al", 1)
                   for i in range(6)]
    calls = {}
    app.play_tracks = lambda **kw: calls.update(kw)
    app.notify = lambda *a, **k: None
    App.skip_to_queue_index(app, 3)
    assert calls["uris"] == [t.uri for t in app.up_next[3:]]
    assert app._staged is False
