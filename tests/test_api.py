"""Tests for zpotify.api — request plumbing, retries, pagination, and parsing.

No real network calls: urllib.request.urlopen and SpotifyAPI._request are
monkeypatched with canned fixtures throughout.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from email.message import Message

import pytest

from zpotify.api import ApiError, SpotifyAPI, parse_album, parse_artist, parse_device, parse_playback, parse_playlist, parse_track


class DummyAuth:
    """Minimal stand-in for Auth: hands out a fixed token, tracks force-refreshes."""

    def __init__(self, token: str = "tok") -> None:
        self.token = token
        self.forced = False

    def get_access_token(self, force: bool = False) -> str:
        if force:
            self.forced = True
        return self.token


def make_http_error(code: int, body: bytes, headers: dict | None = None, reason: str = "Error") -> urllib.error.HTTPError:
    hdrs = Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError(url="https://api.spotify.com/v1/x", code=code, msg=reason, hdrs=hdrs, fp=io.BytesIO(body))


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


# -- parse_track --------------------------------------------------------------------


def test_parse_track_filters_none_and_local():
    assert parse_track(None) is None
    assert parse_track({"is_local": True, "id": "x", "name": "local"}) is None

    t = parse_track(
        {
            "id": "1",
            "uri": "spotify:track:1",
            "name": "Song",
            "artists": [{"name": "A"}, {"name": "B"}],
            "album": {"name": "Album"},
            "duration_ms": 12345,
            "explicit": True,
        }
    )
    assert t is not None
    assert t.id == "1"
    assert t.artists == ("A", "B")
    assert t.album == "Album"
    assert t.explicit is True


# -- parse_playback -------------------------------------------------------------------


def test_parse_playback_with_item_none():
    data = {
        "is_playing": False,
        "progress_ms": None,
        "item": None,
        "device": None,
        "shuffle_state": False,
        "repeat_state": "off",
        "context": None,
    }
    state = parse_playback(data)
    assert state.track is None
    assert state.device is None
    assert state.is_playing is False
    assert state.progress_ms == 0
    assert state.volume_percent is None
    assert state.context_uri is None


def test_parse_playback_with_track_and_device():
    data = {
        "is_playing": True,
        "progress_ms": 5000,
        "item": {
            "id": "1",
            "uri": "spotify:track:1",
            "name": "Song",
            "artists": [{"name": "A"}],
            "album": {"name": "Al"},
            "duration_ms": 200000,
        },
        "device": {"id": "d1", "name": "Laptop", "type": "Computer", "is_active": True, "volume_percent": 70},
        "shuffle_state": True,
        "repeat_state": "track",
        "context": {"uri": "spotify:playlist:xyz"},
    }
    state = parse_playback(data)
    assert state.track is not None and state.track.name == "Song"
    assert state.device is not None and state.device.name == "Laptop"
    assert state.shuffle is True
    assert state.repeat == "track"
    assert state.volume_percent == 70
    assert state.context_uri == "spotify:playlist:xyz"


# -- parse_album / parse_artist / parse_playlist / parse_device -----------------------


def test_parse_playlist_owner_falls_back_to_id():
    p = parse_playlist({"id": "p1", "uri": "spotify:playlist:p1", "name": "Mix", "owner": {"id": "u1"}, "tracks": {"total": 12}})
    assert p is not None
    assert p.owner == "u1"
    assert p.total_tracks == 12


def test_parse_album_and_artist_none_safe():
    assert parse_album(None) is None
    assert parse_artist(None) is None
    assert parse_device(None) is None


# -- search: filters None entries in items ---------------------------------------------


def test_search_filters_none_entries(monkeypatch):
    canned = {
        "tracks": {
            "items": [
                None,
                {"id": "1", "uri": "u1", "name": "T1", "artists": [{"name": "A"}], "album": {"name": "Al"}, "duration_ms": 1},
                {"is_local": True, "id": "2"},
            ]
        },
        "albums": {"items": [None, {"id": "a1", "uri": "au1", "name": "Al1", "artists": [{"name": "A"}]}]},
        "artists": {"items": [None, {"id": "ar1", "uri": "aru1", "name": "Ar1"}]},
        "playlists": {"items": [None]},
    }

    def fake_request(self, method, path, params=None, body=None):
        assert method == "GET"
        assert path == "/search"
        return canned

    monkeypatch.setattr(SpotifyAPI, "_request", fake_request)
    api = SpotifyAPI(DummyAuth())
    results = api.search("query")

    assert len(results.tracks) == 1
    assert results.tracks[0].id == "1"
    assert len(results.albums) == 1
    assert len(results.artists) == 1
    assert len(results.playlists) == 0


# -- play(): URL/query building -----------------------------------------------------


def test_play_context_uri_and_device_id_query(monkeypatch):
    captured = []

    def fake_request(self, method, path, params=None, body=None):
        captured.append({"method": method, "path": path, "params": params, "body": body})
        return None

    monkeypatch.setattr(SpotifyAPI, "_request", fake_request)
    api = SpotifyAPI(DummyAuth())

    api.play(device_id="dev1", context_uri="spotify:album:1")
    call = captured[-1]
    assert call["path"] == "/me/player/play"
    assert call["params"] == {"device_id": "dev1"}
    assert call["body"] == {"context_uri": "spotify:album:1"}


def test_play_uris_and_offset(monkeypatch):
    captured = []

    def fake_request(self, method, path, params=None, body=None):
        captured.append({"params": params, "body": body})
        return None

    monkeypatch.setattr(SpotifyAPI, "_request", fake_request)
    api = SpotifyAPI(DummyAuth())

    api.play(uris=["spotify:track:1", "spotify:track:2"], offset_position=1, position_ms=5000)
    call = captured[-1]
    assert call["body"] == {"uris": ["spotify:track:1", "spotify:track:2"], "offset": {"position": 1}, "position_ms": 5000}
    assert call["params"] == {"device_id": None}


def test_play_no_args_sends_empty_body(monkeypatch):
    captured = []

    def fake_request(self, method, path, params=None, body=None):
        captured.append({"params": params, "body": body})
        return None

    monkeypatch.setattr(SpotifyAPI, "_request", fake_request)
    api = SpotifyAPI(DummyAuth())
    api.play()
    assert captured[-1]["body"] is None


# -- _request: query building against real urlopen (mocked) ---------------------------


def test_request_query_string_supports_list_values_and_omits_none(monkeypatch):
    captured = {}

    def fake_urlopen(req, *a, **kw):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return FakeResponse(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    api = SpotifyAPI(DummyAuth())
    api._request("GET", "/search", params={"q": "test", "type": None, "ids": ["a", "b"]})

    assert "q=test" in captured["url"]
    assert "type" not in captured["url"]
    assert "ids=a" in captured["url"]
    assert "ids=b" in captured["url"]
    assert captured["method"] == "GET"


def test_request_empty_body_returns_none(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, *a, **kw: FakeResponse(b""))
    api = SpotifyAPI(DummyAuth())
    assert api._request("PUT", "/me/player/pause") is None


def test_request_raises_api_error_with_parsed_message(monkeypatch):
    def fake_urlopen(req, *a, **kw):
        raise make_http_error(400, json.dumps({"error": {"status": 400, "message": "Bad request"}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    api = SpotifyAPI(DummyAuth())
    with pytest.raises(ApiError) as exc_info:
        api._request("GET", "/me")
    assert exc_info.value.status == 400
    assert exc_info.value.message == "Bad request"


# -- 401: forces refresh once and retries ------------------------------------------------


def test_401_forces_refresh_and_retries(monkeypatch):
    calls = {"n": 0}
    auth = DummyAuth()

    def fake_urlopen(req, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise make_http_error(401, b'{"error":{"status":401,"message":"expired"}}')
        return FakeResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    api = SpotifyAPI(auth)
    result = api._request("GET", "/me")

    assert result == {"ok": True}
    assert auth.forced is True
    assert calls["n"] == 2


def test_401_twice_raises_api_error(monkeypatch):
    def fake_urlopen(req, *a, **kw):
        raise make_http_error(401, b'{"error":{"status":401,"message":"still expired"}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    api = SpotifyAPI(DummyAuth())
    with pytest.raises(ApiError) as exc_info:
        api._request("GET", "/me")
    assert exc_info.value.status == 401


# -- 429: retries honoring Retry-After, capped at 30s, up to 3 retries ------------------


def test_429_retry_honors_retry_after(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    def fake_urlopen(req, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise make_http_error(429, b'{"error":{"status":429,"message":"slow down"}}', {"Retry-After": "2"})
        return FakeResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    api = SpotifyAPI(DummyAuth())
    result = api._request("GET", "/me")

    assert result == {"ok": True}
    assert sleeps == [2]
    assert calls["n"] == 2


def test_429_retry_after_capped_at_30(monkeypatch):
    sleeps = []

    def fake_urlopen(req, *a, **kw):
        if len(sleeps) == 0:
            raise make_http_error(429, b"{}", {"Retry-After": "9999"})
        return FakeResponse(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    api = SpotifyAPI(DummyAuth())
    api._request("GET", "/me")
    assert sleeps == [30]


def test_429_gives_up_after_three_retries(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, *a, **kw):
        calls["n"] += 1
        raise make_http_error(429, b'{"error":{"status":429,"message":"nope"}}', {"Retry-After": "0"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    api = SpotifyAPI(DummyAuth())
    with pytest.raises(ApiError):
        api._request("GET", "/me")
    assert calls["n"] == 4  # first attempt + 3 retries


# -- 5xx: two retries with backoff (Spotify 5xxes in bursts) ------------------------------


def test_5xx_retries_twice_with_backoff(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    def fake_urlopen(req, *a, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise make_http_error(500, b'{"error":{"status":500,"message":"oops"}}')
        return FakeResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    api = SpotifyAPI(DummyAuth())
    result = api._request("GET", "/me")

    assert result == {"ok": True}
    assert sleeps == [0.8, 2.0]


def test_5xx_gives_up_after_two_retries(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, *a, **kw):
        calls["n"] += 1
        raise make_http_error(503, b'{"error":{"status":503,"message":"down"}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    api = SpotifyAPI(DummyAuth())
    with pytest.raises(ApiError):
        api._request("GET", "/me")
    assert calls["n"] == 3  # initial + two retries


# -- pagination: stops on next=None, respects caps ----------------------------------------


def test_my_playlists_stops_on_next_none(monkeypatch):
    pages = [
        {
            "items": [{"id": "1", "uri": "u1", "name": "P1", "owner": {"display_name": "me"}, "tracks": {"total": 5}}],
            "next": "https://api.spotify.com/v1/me/playlists?offset=1",
        },
        {
            "items": [{"id": "2", "uri": "u2", "name": "P2", "owner": {"display_name": "me"}, "tracks": {"total": 3}}],
            "next": None,
        },
    ]
    calls = []

    def fake_request(self, method, path, params=None, body=None):
        calls.append(params)
        return pages.pop(0)

    monkeypatch.setattr(SpotifyAPI, "_request", fake_request)
    api = SpotifyAPI(DummyAuth())
    result = api.my_playlists(limit=1)

    assert [p.id for p in result] == ["1", "2"]
    assert len(calls) == 2


def test_playlist_tracks_uses_items_endpoint_and_filters(monkeypatch):
    # 2026 dev-mode API: /playlists/{id}/items with an "item" wrapper key
    def track(id_):
        return {"type": "track", "episode": False, "id": id_, "uri": f"u{id_}",
                "name": "S", "artists": [{"name": "A"}],
                "album": {"name": "Al"}, "duration_ms": 1000}

    page = {
        "items": [
            None,
            {"item": track("1")},
            {"item": {**track("ep"), "type": "episode", "episode": True}},
            {"is_local": True, "item": track("loc")},
            {"track": track("2")},  # legacy wrapper key still parses
            {"item": None},
        ],
        "next": None,
    }
    paths = []

    def fake_request(self, method, path, params=None, body=None):
        paths.append(path)
        return page

    monkeypatch.setattr(SpotifyAPI, "_request", fake_request)
    api = SpotifyAPI(DummyAuth())
    tracks = api.playlist_tracks("pl1")

    assert paths == ["/playlists/pl1/items"]
    assert [t.id for t in tracks] == ["1", "2"]


def test_parse_playlist_reads_renamed_items_stub():
    p = parse_playlist({"id": "p1", "uri": "spotify:playlist:p1", "name": "Mix",
                        "owner": {"id": "u1"}, "items": {"total": 42}})
    assert p is not None and p.total_tracks == 42


def test_queue_parses_queue_key(monkeypatch):
    data = {
        "queue": [
            {"id": "1", "uri": "u1", "name": "S1", "artists": [{"name": "A"}], "album": {"name": "Al"}, "duration_ms": 1},
            None,
        ]
    }
    monkeypatch.setattr(SpotifyAPI, "_request", lambda self, method, path, params=None, body=None: data)
    api = SpotifyAPI(DummyAuth())
    result = api.queue()
    assert [t.id for t in result] == ["1"]
