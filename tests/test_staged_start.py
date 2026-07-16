"""Tests for startup staging: local zpotify session vs Spotify history."""

from __future__ import annotations

import io
import time

import pytest

from zpotify import config as cfg
from zpotify.auth import Auth
from zpotify.models import Device, PlaybackState, Track
from zpotify.term.screen import Screen
from zpotify.ui.app import App, choose_stage

TRACK = Track("t1", "spotify:track:t1", "Time", ("Hans Zimmer",), "Inception", 275000)
QUEUED = Track("t2", "spotify:track:t2", "Strobe", ("deadmau5",), "x", 634000)


def local_session(saved_at: float, context: str | None = None) -> dict:
    return {"saved_at": saved_at, "track": TRACK.to_dict(),
            "progress_ms": 61000, "context_uri": context,
            "up_next": [QUEUED.to_dict()]}


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


# -- choosing what to stage ----------------------------------------------------

def test_newer_local_session_beats_history() -> None:
    remote = (QUEUED, 1000.0)
    picked = choose_stage(local_session(saved_at=2000.0), remote)
    assert picked is not None and picked["kind"] == "local"
    assert picked["track"]["id"] == "t1"
    assert picked["up_next"][0]["id"] == "t2"


def test_newer_history_beats_stale_local() -> None:
    remote = (QUEUED, 5000.0)
    picked = choose_stage(local_session(saved_at=1000.0), remote)
    assert picked is not None and picked["kind"] == "remote"
    assert picked["track"]["id"] == "t2"
    assert picked["up_next"] == []


def test_tie_goes_to_local_and_missing_sides_degrade() -> None:
    assert choose_stage(local_session(3000.0), (QUEUED, 3000.0))["kind"] == "local"
    assert choose_stage(None, (QUEUED, 1.0))["kind"] == "remote"
    assert choose_stage(local_session(1.0), None)["kind"] == "local"
    assert choose_stage(None, None) is None


# -- staging behavior -----------------------------------------------------------

def test_stage_restores_track_progress_and_up_next(app) -> None:
    app._on_stage({"kind": "local", **local_session(9e9)}, None)
    assert app._staged
    assert app.playback.track.id == "t1"
    assert app.playback.progress_ms == 61000    # position kept for display
    assert [t.id for t in app.up_next] == ["t2"]  # queue restored on screen
    app._on_playback(None, None)                # empty polls don't clear it
    assert app.playback is not None and app.playback.track.id == "t1"


def test_space_restores_context_session(app, monkeypatch) -> None:
    app._on_stage({"kind": "local",
                   **local_session(9e9, context="spotify:playlist:p1")}, None)
    calls = {}
    monkeypatch.setattr(app, "play_tracks", lambda **kw: calls.update(kw))
    app.toggle_play()
    assert calls["context_uri"] == "spotify:playlist:p1"
    assert calls["offset_uri"] == TRACK.uri
    assert calls["position_ms"] == 61000
    assert not app._staged


def test_space_chains_up_next_without_context(app, monkeypatch) -> None:
    app._on_stage({"kind": "local", **local_session(9e9)}, None)
    calls = {}
    monkeypatch.setattr(app, "play_tracks", lambda **kw: calls.update(kw))
    app.toggle_play()
    assert calls["uris"] == [TRACK.uri, QUEUED.uri]  # session recreated whole
    assert calls["position_ms"] == 61000


def test_real_playback_replaces_staged(app) -> None:
    app._on_stage({"kind": "remote", "track": TRACK.to_dict(),
                   "progress_ms": 0, "context_uri": None, "up_next": []}, None)
    live = PlaybackState(is_playing=True, progress_ms=5, track=QUEUED)
    app._on_playback(live, None)
    assert not app._staged and app.playback is live


# -- session persistence ---------------------------------------------------------

def test_session_saved_only_for_our_device(app) -> None:
    ours = Device("dev1", "zpotify", "computer", True, 80)
    theirs = Device("phone", "Phone", "smartphone", True, 80)

    app.playback = PlaybackState(is_playing=True, progress_ms=1000, track=TRACK,
                                 device=theirs, context_uri=None)
    app._poll_at = time.monotonic()
    app._maybe_save_session()
    assert cfg.read_session() is None           # phone sessions are not ours

    app.playback = PlaybackState(is_playing=True, progress_ms=1000, track=TRACK,
                                 device=ours, context_uri="spotify:album:a1")
    app.up_next = [QUEUED]
    app._maybe_save_session()
    saved = cfg.read_session()
    assert saved is not None
    assert saved["track"]["id"] == "t1"
    assert saved["context_uri"] == "spotify:album:a1"
    assert saved["up_next"][0]["id"] == "t2"
    assert saved["saved_at"] > 0


# -- radio fill for an empty staged queue -----------------------------------------

RADIO = [Track(f"r{i}", f"spotify:track:r{i}", f"R{i}", ("A",), "Al", 1000)
         for i in range(12)]


def _stage_remote(app):
    app._on_stage({"kind": "remote", "track": TRACK.to_dict(),
                   "progress_ms": 0, "context_uri": None, "up_next": []}, None)


def test_empty_staged_queue_requests_radio(app, monkeypatch) -> None:
    submitted = []
    monkeypatch.setattr(app.workers, "submit",
                        lambda fn, cb=None: submitted.append((fn, cb)))
    _stage_remote(app)
    assert any(fn == app._fetch_radio for fn, _ in submitted)
    app._on_radio(RADIO[:10], None)
    assert len(app.up_next) == 10
    assert app.up_next_is_radio


def test_radio_failure_schedules_retry_then_gives_up(app) -> None:
    _stage_remote(app)
    for _ in range(2):
        app._on_radio(None, RuntimeError("503"))
        assert app._radio_retry_at is not None
        app._radio_retry_at = None
    app._on_radio(None, RuntimeError("503"))
    assert app._radio_retry_at is None  # third strike: stop retrying


def test_queue_poll_does_not_clobber_staged_radio(app) -> None:
    _stage_remote(app)
    app._on_radio(RADIO[:10], None)
    app._on_queue([], None)  # dead session's queue is empty — must not wipe
    assert len(app.up_next) == 10 and app.up_next_is_radio


def test_enter_on_staged_radio_row_starts_chain_there(app, monkeypatch) -> None:
    _stage_remote(app)
    app._on_radio(RADIO[:10], None)
    calls = {}
    monkeypatch.setattr(app, "play_tracks", lambda **kw: calls.update(kw))
    app.skip_to_queue_index(3)
    assert calls["uris"][0] == RADIO[3].uri  # starts at the chosen row
    assert len(calls["uris"]) == 7
    assert not app._staged


def test_fetch_radio_excludes_current_and_dedupes(app, monkeypatch) -> None:
    _stage_remote(app)
    class R:
        tracks = [TRACK, RADIO[0], RADIO[0], *RADIO[1:12]]
    monkeypatch.setattr(app.api, "search", lambda q, limit=20: R())
    radio = app._fetch_radio()
    assert TRACK.id not in [t.id for t in radio]
    assert len(radio) == 10
    assert len({t.id for t in radio}) == 10


def test_play_tracks_filters_malformed_uris(app, monkeypatch) -> None:
    calls = {}
    monkeypatch.setattr(app.api, "play", lambda **kw: calls.update(kw))
    # run the queued call synchronously instead of on the control pool
    monkeypatch.setattr(app, "call_api",
                        lambda fn, then=None, refresh=True, describe="",
                        on_error=None: fn())
    app.play_tracks(uris=["spotify:track:good1", "", "spotify:track:",
                          None, "spotify:episode:x", "spotify:track:good2"])
    assert calls["uris"] == ["spotify:track:good1", "spotify:track:good2"]

    notes = []
    monkeypatch.setattr(app, "notify", lambda m, error=False: notes.append(m))
    calls.clear()
    app.play_tracks(uris=["", None])
    assert not calls                     # nothing sent to Spotify
    assert "nothing playable" in notes[0]


# -- crossfade-aware track display ------------------------------------------------

def _mk_track(i: int) -> Track:
    return Track(f"t{i}", f"spotify:track:{'z'*18}{i:04d}", f"S{i}", ("A",), "Al", 200000)

def test_track_change_display_waits_for_audible_boundary(app) -> None:
    a, b = _mk_track(1), _mk_track(2)
    from zpotify.models import PlaybackState
    app._adopt_playback(PlaybackState(is_playing=True, progress_ms=1000, track=a),
                        time.monotonic())
    # librespot streamed ahead: boundary marked but not yet audible
    app.audio.set_crossfade(2.0)
    app.audio._boundaries.append(10_000)

    late = PlaybackState(is_playing=True, progress_ms=5, track=b)
    app._on_playback(late, None)
    assert app.playback.track.id == a.id      # still showing the outgoing track
    assert app._pending_playback is not None

    app.audio._boundaries.clear()             # boundary reached the speakers
    app._tick(time.monotonic())
    assert app.playback.track.id == b.id      # now adopted
    assert app._pending_playback is None


def test_pending_track_discarded_after_user_action(app) -> None:
    a, b = _mk_track(1), _mk_track(2)
    from zpotify.models import PlaybackState
    app._adopt_playback(PlaybackState(is_playing=True, progress_ms=1000, track=a),
                        time.monotonic())
    app.audio.set_crossfade(2.0)
    app.audio._boundaries.append(10_000)
    app._on_playback(PlaybackState(is_playing=True, progress_ms=5, track=b), None)
    assert app._pending_playback is not None
    app._mark_action()                        # user skipped/sought meanwhile
    app.audio._boundaries.clear()
    app._tick(time.monotonic())
    assert app._pending_playback is None
    assert app.playback.track.id == a.id      # stale snapshot not adopted
