"""Tests for the app's handling of Spotify rate limits and adaptive polling.

No network: the API's block table is set directly and worker submissions are
recorded instead of run.
"""

from __future__ import annotations

import io
import time

import pytest

from zpotify import config as cfg
from zpotify.api import RateLimited, _bucket
from zpotify.auth import Auth
from zpotify.models import PlaybackState, Track
from zpotify.term.screen import Screen
from zpotify.ui import app as app_mod
from zpotify.ui.app import App


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cfg, "TOKENS_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(cfg, "SESSION_FILE", tmp_path / "session.json")
    config = cfg.Config(client_id="x" * 32)
    a = App(config, Auth(config))
    a.screen = Screen(out=io.StringIO(), size=(102, 27))
    a.submitted = []
    monkeypatch.setattr(a.workers, "submit", lambda fn, cb: a.submitted.append(fn))
    return a


def _block_player(app, seconds: float) -> None:
    app.api._blocked_until[_bucket("/me/player")] = time.monotonic() + seconds


def _track(duration_ms: int = 200_000) -> Track:
    return Track("t1", "spotify:track:" + "a" * 22, "Song", ("A",), "Al", duration_ms)


# -- surfacing the real reason --------------------------------------------------------

def test_discovery_rate_limited_explains_and_schedules_retry(app) -> None:
    before = time.monotonic()
    app._on_device_found(None, RateLimited(3600))
    assert app._status_error
    assert "rate-limited" in app._status and "1h 00m" in app._status
    assert app._rediscover_at is not None
    assert app._rediscover_at >= before + 3600


def test_play_while_rate_limited_says_so(app) -> None:
    _block_player(app, 44001)
    app.play_tracks(uris=["spotify:track:" + "q" * 22])
    assert "rate-limited" in app._status
    assert "not ready" not in app._status


def test_play_without_device_otherwise_says_not_ready(app) -> None:
    app.play_tracks(uris=["spotify:track:" + "q" * 22])
    assert app._status == "player device not ready yet"


def test_other_discovery_errors_retry_later(app) -> None:
    app._on_device_found(None, RuntimeError("boom"))
    assert app._rediscover_at is not None


# -- backoff ---------------------------------------------------------------------------

def test_no_player_polls_while_blocked(app) -> None:
    app.device_id = "dev1"
    _block_player(app, 600)
    app._tick(time.monotonic())
    assert app.api.playback not in app.submitted
    assert app.api.queue not in app.submitted


def test_rediscovery_fires_once_block_lifts(app) -> None:
    app._on_device_found(None, RateLimited(5))
    app._tick(time.monotonic())
    assert app._find_our_device not in app.submitted
    app._tick(app._rediscover_at + 0.1)
    assert app._find_our_device in app.submitted
    assert app._rediscover_at is None


# -- adaptive polling ------------------------------------------------------------------

def test_poll_fast_after_activity_slow_when_idle(app) -> None:
    now = time.monotonic()
    app._activity_at = now
    assert app.poll_interval(now) == app_mod.POLL_ACTIVE
    assert app.poll_interval(now + app_mod.ACTIVE_WINDOW + 1) == app_mod.POLL_IDLE


def test_idle_poll_wakes_at_track_end(app) -> None:
    now = time.monotonic()
    app._poll_at = now
    app.playback = PlaybackState(is_playing=True, progress_ms=196_000, track=_track())
    later = now + app_mod.ACTIVE_WINDOW + 1   # idle; track nearly over
    interval = app.poll_interval(later)
    assert interval < app_mod.POLL_IDLE
    assert interval >= app_mod.POLL_ACTIVE


def test_local_action_counts_as_activity(app) -> None:
    app._mark_action()
    assert app.poll_interval(time.monotonic()) == app_mod.POLL_ACTIVE
