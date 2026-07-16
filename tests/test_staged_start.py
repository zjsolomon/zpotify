"""Tests for the staged startup track (last played anywhere, ready to play)."""

from __future__ import annotations

import io

import pytest

from zpotify import config as cfg
from zpotify.auth import Auth
from zpotify.models import PlaybackState, Track
from zpotify.term.screen import Screen
from zpotify.ui.app import App

TRACK = Track("t1", "spotify:track:t1", "Time", ("Hans Zimmer",), "Inception", 275000)


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cfg, "TOKENS_FILE", tmp_path / "tokens.json")
    config = cfg.Config(client_id="x" * 32)
    a = App(config, Auth(config))
    a.screen = Screen(out=io.StringIO(), size=(102, 27))
    a.device_id = "dev1"
    return a


def test_empty_poll_stages_recent_track(app, monkeypatch) -> None:
    fetched = []
    monkeypatch.setattr(app.workers, "submit",
                        lambda fn, cb=None: fetched.append((fn, cb)))
    app._on_playback(None, None)          # nothing playing anywhere
    assert app._stage_attempted
    assert fetched                         # recently-played fetch queued
    app._on_recent([TRACK], None)
    assert app._staged
    assert app.playback is not None and app.playback.track == TRACK
    assert app.playback.is_playing is False


def test_staged_track_survives_empty_polls(app) -> None:
    app._on_recent([TRACK], None)
    app._staged = True
    app._on_playback(None, None)           # poll still says nothing playing
    assert app.playback is not None and app.playback.track == TRACK


def test_space_plays_staged_track_fresh(app, monkeypatch) -> None:
    app._on_recent([TRACK], None)
    played = {}
    monkeypatch.setattr(app, "play_tracks",
                        lambda uris=None, **kw: played.update(uris=uris))
    app.toggle_play()
    assert played["uris"] == [TRACK.uri]   # started as a new session, not resume
    assert not app._staged


def test_real_playback_replaces_staged(app) -> None:
    app._on_recent([TRACK], None)
    other = Track("t2", "spotify:track:t2", "Strobe", ("deadmau5",), "x", 1000)
    live = PlaybackState(is_playing=True, progress_ms=5, track=other)
    app._on_playback(live, None)
    assert not app._staged
    assert app.playback is live


def test_no_staging_when_something_is_playing(app, monkeypatch) -> None:
    live = PlaybackState(is_playing=True, progress_ms=5, track=TRACK)
    app._on_playback(live, None)
    app._on_recent([TRACK], None)          # late fetch must not clobber
    assert app.playback is live
    assert not app._staged
