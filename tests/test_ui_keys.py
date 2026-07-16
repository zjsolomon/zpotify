"""Tests for global tab-switching keys, the search view's focus behavior, and
the floating search overlay (/)."""

from __future__ import annotations

import io

import pytest

from zpotify import config as cfg
from zpotify.auth import Auth
from zpotify.models import Track
from zpotify.term.events import Key
from zpotify.term.screen import Screen
from zpotify.ui.app import App
from zpotify.ui.views.search import SearchView

TRACK = Track("t1", "spotify:track:t1", "Time", ("Hans Zimmer",), "Inception", 275000)
TRACK2 = Track("t2", "spotify:track:t2", "Strobe", ("deadmau5",), "x", 634000)


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cfg, "TOKENS_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(cfg, "SESSION_FILE", tmp_path / "session.json")
    config = cfg.Config(client_id="x" * 32)
    a = App(config, Auth(config))
    a.screen = Screen(out=io.StringIO(), size=(102, 27))
    a.device_id = "dev1"
    return a


# -- tab/shift+tab tab switching ----------------------------------------------

def test_tab_advances_to_next_view(app) -> None:
    assert app.view_index == 0
    app._handle_key(Key(name="tab"))
    assert app.view_index == 1


def test_backtab_wraps_from_first_to_last_view(app) -> None:
    assert app.view_index == 0
    app._handle_key(Key(name="backtab"))
    assert app.view_index == len(app.views) - 1


def test_tab_wraps_from_last_to_first_view(app) -> None:
    app.view_index = len(app.views) - 1
    app._handle_key(Key(name="tab"))
    assert app.view_index == 0


def test_number_key_switches_view(app) -> None:
    app._handle_key(Key(char="3"))
    assert app.view_index == 2


# -- search view focus behavior -----------------------------------------------

def test_switching_to_search_does_not_auto_focus(app) -> None:
    app._handle_key(Key(name="tab"))
    assert app.view_index == 1
    assert app.views[1].focused is False


def test_switching_to_search_via_number_does_not_auto_focus(app) -> None:
    app._handle_key(Key(char="2"))
    assert app.view_index == 1
    assert app.views[1].focused is False


def test_enter_with_no_results_focuses_search_input(app) -> None:
    app._handle_key(Key(char="2"))
    view = app.views[1]
    assert not view.tracks.rows
    app._handle_key(Key(name="enter"))
    assert view.focused is True


def test_typing_after_enter_focus_lands_in_input(app) -> None:
    app._handle_key(Key(char="2"))
    app._handle_key(Key(name="enter"))
    view = app.views[1]
    assert view.focused is True
    app._handle_key(Key(char="x"))
    app._handle_key(Key(char="y"))
    assert view.query.value == "xy"


def test_enter_with_results_plays_instead_of_focusing(app, monkeypatch) -> None:
    app._handle_key(Key(char="2"))
    view = app.views[1]
    view.tracks.rows = [TRACK, TRACK2]
    calls = []
    monkeypatch.setattr(app, "play_tracks", lambda **kw: calls.append(kw))
    app._handle_key(Key(name="enter"))
    assert view.focused is False
    assert calls


# -- floating search overlay ---------------------------------------------------

def test_slash_opens_overlay(app) -> None:
    assert app.search_overlay is None
    app._handle_key(Key(char="/"))
    assert app.search_overlay is not None


def test_typed_chars_land_in_overlay(app) -> None:
    app._handle_key(Key(char="/"))
    app._handle_key(Key(char="a"))
    app._handle_key(Key(char="b"))
    assert app.search_overlay.value == "ab"


def test_slash_again_closes_without_searching(app, monkeypatch) -> None:
    app._handle_key(Key(char="/"))
    app._handle_key(Key(char="z"))
    searched = []
    monkeypatch.setattr(SearchView, "_search", lambda self, app: searched.append(True))
    app._handle_key(Key(char="/"))
    assert app.search_overlay is None
    assert not searched
    assert app.view_index == 0


def test_esc_closes_overlay(app) -> None:
    app._handle_key(Key(char="/"))
    app._handle_key(Key(name="esc"))
    assert app.search_overlay is None


def test_overlay_enter_runs_search_and_switches_to_search_view(app, monkeypatch) -> None:
    app._handle_key(Key(char="/"))
    for ch in "abc":
        app._handle_key(Key(char=ch))
    searched = []
    monkeypatch.setattr(app.views[1], "_search", lambda a: searched.append(a))
    app._handle_key(Key(name="enter"))
    assert app.search_overlay is None
    assert app.view_index == 1
    assert app.views[1].query.value == "abc"
    assert app.views[1].query.cursor == 3
    assert app.views[1].focused is False
    assert searched == [app]


def test_overlay_enter_with_empty_text_just_closes(app, monkeypatch) -> None:
    app._handle_key(Key(char="/"))
    searched = []
    monkeypatch.setattr(app.views[1], "_search", lambda a: searched.append(a))
    app._handle_key(Key(name="enter"))
    assert app.search_overlay is None
    assert app.view_index == 0
    assert not searched


def test_h_and_l_type_into_overlay_instead_of_switching_tabs(app) -> None:
    app._handle_key(Key(char="/"))
    app._handle_key(Key(char="h"))
    app._handle_key(Key(char="l"))
    assert app.search_overlay.value == "hl"
    assert app.view_index == 0


# -- vim keys restored inside views ---------------------------------------------

def test_h_backs_out_of_playlist_tracks(app) -> None:
    view = app.views[2]
    view.mode = "tracks"
    app.view_index = 2
    app._handle_key(Key(char="h"))
    assert view.mode == "lists"
    assert app.view_index == 2  # stayed on the tab


def test_h_l_cycle_setting_values(app) -> None:
    view = app.views[6]
    view.on_show(app)
    app.view_index = 6
    setting = view.listview.rows[0]
    before = setting.get()
    app._handle_key(Key(char="l"))
    assert setting.get() != before
    app._handle_key(Key(char="h"))
    assert setting.get() == before


# -- live queue top-up ------------------------------------------------------------

def _mk_track(i: int):
    from zpotify.models import Track
    return Track(f"t{i}", f"spotify:track:{'y'*18}{i:04d}", f"S{i}", ("Artist",), "Al", 1000)


def test_short_queue_tops_up_from_filler_pool(app) -> None:
    from zpotify.models import PlaybackState
    current = _mk_track(0)
    app.playback = PlaybackState(is_playing=True, progress_ms=0, track=current)
    app._filler_key = "Artist"
    app._filler_pool = [_mk_track(i) for i in range(20, 35)]
    app._on_queue([_mk_track(1)], None)  # live queue nearly empty
    assert len(app.up_next) == 10        # padded to full
    assert app.up_next[0].id == "t1"     # real queue item stays first


def test_full_queue_untouched_and_fillers_deduped(app) -> None:
    from zpotify.models import PlaybackState
    app.playback = PlaybackState(is_playing=True, progress_ms=0, track=_mk_track(0))
    app._filler_key = "Artist"
    app._filler_pool = [_mk_track(1), _mk_track(50)]  # t1 dupes the live queue
    live = [_mk_track(i) for i in range(1, 11)]
    app._on_queue(live, None)
    assert app.up_next == live           # already full: no padding

    app._on_queue(live[:2], None)        # short queue: pad, but skip dupe t1
    ids = [t.id for t in app.up_next]
    assert ids[:2] == ["t1", "t2"] and "t50" in ids and ids.count("t1") == 1


def test_empty_pool_triggers_filler_fetch(app, monkeypatch) -> None:
    from zpotify.models import PlaybackState
    app.playback = PlaybackState(is_playing=True, progress_ms=0, track=_mk_track(0))
    submitted = []
    monkeypatch.setattr(app.workers, "submit", lambda fn, cb=None: submitted.append(fn))
    app._on_queue([_mk_track(1)], None)
    assert app._filler_fetching and submitted
    app._filler_fetching = False
    app._on_filler(("Artist", [_mk_track(i) for i in range(60, 75)]), None)
    assert len(app.up_next) == 10
