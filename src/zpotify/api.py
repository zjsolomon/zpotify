"""Thin client for the Spotify Web API, built on urllib only.

Parsing helpers turn raw Spotify JSON into the dataclasses from models.py;
the public SpotifyAPI methods stay small and typed.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

from zpotify.auth import Auth
from zpotify.models import (
    Album,
    Artist,
    Device,
    Playlist,
    PlaybackState,
    SearchResults,
    Track,
)

API_BASE = "https://api.spotify.com/v1"

_MAX_429_RETRIES = 3
_MAX_RETRY_AFTER_SECONDS = 30


class ApiError(Exception):
    """A Spotify Web API request failed."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class SpotifyAPI:
    """Typed wrapper over the subset of the Spotify Web API zpotify needs."""

    def __init__(self, auth: Auth) -> None:
        self._auth = auth

    # -- core request plumbing -------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        _retries_429: int = 0,
        _retried_401: bool = False,
        _retried_5xx: int = 0,
    ) -> dict | None:
        url = f"{API_BASE}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean, doseq=True)}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        token = self._auth.get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                if raw[:2] == b"\x1f\x8b":  # gzip body urllib didn't decode
                    import gzip
                    raw = gzip.decompress(raw)
                if not raw.strip():
                    return None
                try:
                    return json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Player commands sometimes 200 with a junk body; the
                    # command still succeeded — don't surface a parse error.
                    return None
        except urllib.error.HTTPError as exc:
            return self._handle_error(
                exc, method, path, params, body, _retries_429, _retried_401, _retried_5xx
            )
        except urllib.error.URLError as exc:
            raise ApiError(0, str(exc.reason)) from exc

    def _handle_error(
        self,
        exc: urllib.error.HTTPError,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        body: dict[str, Any] | None,
        retries_429: int,
        retried_401: bool,
        retried_5xx: int,
    ) -> dict | None:
        raw = exc.read()
        status = exc.code

        if status == 401 and not retried_401:
            self._auth.get_access_token(force=True)
            return self._request(
                method, path, params, body, retries_429, True, retried_5xx
            )

        if status == 429 and retries_429 < _MAX_429_RETRIES:
            retry_after = exc.headers.get("Retry-After", "1") if exc.headers else "1"
            try:
                wait = min(int(retry_after), _MAX_RETRY_AFTER_SECONDS)
            except ValueError:
                wait = 1
            time.sleep(wait)
            return self._request(
                method, path, params, body, retries_429 + 1, retried_401, retried_5xx
            )

        # Spotify's dev-mode API throws intermittent 5xx bursts; two retries
        # with a short backoff ride most of them out.
        if 500 <= status < 600 and retried_5xx < 2:
            time.sleep(0.8 if retried_5xx == 0 else 2.0)
            return self._request(
                method, path, params, body, retries_429, retried_401, retried_5xx + 1
            )

        message = _error_message(raw, exc.reason)
        raise ApiError(status, message)

    # -- playback ---------------------------------------------------------------

    def me(self) -> dict:
        return self._request("GET", "/me") or {}

    def devices(self) -> list[Device]:
        data = self._request("GET", "/me/player/devices") or {}
        return [parse_device(d) for d in data.get("devices", [])]

    def playback(self) -> PlaybackState | None:
        data = self._request("GET", "/me/player")
        if data is None:
            return None
        return parse_playback(data)

    def transfer(self, device_id: str, play: bool = False) -> None:
        self._request("PUT", "/me/player", body={"device_ids": [device_id], "play": play})

    def play(
        self,
        device_id: str | None = None,
        context_uri: str | None = None,
        uris: list[str] | None = None,
        offset_position: int | None = None,
        offset_uri: str | None = None,
        position_ms: int | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if context_uri is not None:
            body["context_uri"] = context_uri
        if uris is not None:
            body["uris"] = uris
        if offset_position is not None:
            body["offset"] = {"position": offset_position}
        elif offset_uri is not None:
            body["offset"] = {"uri": offset_uri}
        if position_ms is not None:
            body["position_ms"] = position_ms
        self._request(
            "PUT", "/me/player/play", params={"device_id": device_id}, body=body or None
        )

    def pause(self, device_id: str | None = None) -> None:
        self._request("PUT", "/me/player/pause", params={"device_id": device_id})

    def next_track(self, device_id: str | None = None) -> None:
        self._request("POST", "/me/player/next", params={"device_id": device_id})

    def previous_track(self, device_id: str | None = None) -> None:
        self._request("POST", "/me/player/previous", params={"device_id": device_id})

    def seek(self, position_ms: int, device_id: str | None = None) -> None:
        self._request(
            "PUT",
            "/me/player/seek",
            params={"position_ms": position_ms, "device_id": device_id},
        )

    def set_volume(self, percent: int, device_id: str | None = None) -> None:
        self._request(
            "PUT",
            "/me/player/volume",
            params={"volume_percent": percent, "device_id": device_id},
        )

    def set_shuffle(self, state: bool, device_id: str | None = None) -> None:
        self._request(
            "PUT",
            "/me/player/shuffle",
            params={"state": str(state).lower(), "device_id": device_id},
        )

    def set_repeat(self, mode: str, device_id: str | None = None) -> None:
        self._request(
            "PUT", "/me/player/repeat", params={"state": mode, "device_id": device_id}
        )

    def queue(self) -> list[Track]:
        data = self._request("GET", "/me/player/queue") or {}
        tracks = (parse_track(t) for t in data.get("queue", []))
        return [t for t in tracks if t is not None]

    def add_to_queue(self, uri: str, device_id: str | None = None) -> None:
        self._request(
            "POST", "/me/player/queue", params={"uri": uri, "device_id": device_id}
        )

    # -- search / library ---------------------------------------------------------

    # Spotify's 2026 dev-mode API rejects search limits above 10
    _SEARCH_PAGE = 10

    def search(
        self,
        q: str,
        types: Iterable[str] = ("track", "album", "artist", "playlist"),
        limit: int = 30,
        offset: int = 0,
    ) -> SearchResults:
        """Search. ``limit`` applies to tracks, fetched in pages of 10; the
        other types get one page of 10 each."""
        types = tuple(types)
        params = {
            "q": q,
            "type": ",".join(types),
            "limit": min(limit, self._SEARCH_PAGE),
            "offset": offset,
        }
        data = self._request("GET", "/search", params=params) or {}

        tracks = [t for t in (parse_track(i) for i in data.get("tracks", {}).get("items", []) or []) if t]
        albums = [a for a in (parse_album(i) for i in data.get("albums", {}).get("items", []) or []) if a]
        artists = [a for a in (parse_artist(i) for i in data.get("artists", {}).get("items", []) or []) if a]
        playlists = [
            p for p in (parse_playlist(i) for i in data.get("playlists", {}).get("items", []) or []) if p
        ]

        # Page tracks up to `limit`. Quirks: single-type searches sometimes
        # get rejected outright (so keep two types and ignore the second),
        # and the endpoint 5xxes in bursts (so partial results beat none).
        page = data.get("tracks", {})
        consumed = len(page.get("items", []) or [])  # raw items, incl. filtered
        while "track" in types and len(tracks) < limit and page.get("next"):
            try:
                data = self._request("GET", "/search", params={
                    "q": q, "type": "track,artist", "limit": self._SEARCH_PAGE,
                    "offset": offset + consumed}) or {}
            except ApiError:
                break  # keep what we already have
            page = data.get("tracks", {})
            items = page.get("items", []) or []
            if not items:
                break
            consumed += len(items)
            tracks.extend(t for t in (parse_track(i) for i in items) if t)
        # Spotify's search pages overlap; drop repeated ids, keep order
        seen: set[str] = set()
        tracks = [t for t in tracks if not (t.id in seen or seen.add(t.id))]
        return SearchResults(tracks=tracks[:limit], albums=albums,
                             artists=artists, playlists=playlists)

    def my_playlists(self, limit: int = 50) -> list[Playlist]:
        playlists: list[Playlist] = []
        offset = 0
        while len(playlists) < 200:
            data = self._request("GET", "/me/playlists", params={"limit": limit, "offset": offset}) or {}
            items = data.get("items", []) or []
            playlists.extend(p for p in (parse_playlist(i) for i in items) if p is not None)
            if data.get("next") is None or not items:
                break
            offset += limit
        return playlists[:200]

    def playlist_tracks(self, playlist_id: str, limit: int = 100) -> list[Track]:
        """List a playlist's tracks.

        Uses ``/playlists/{id}/items``: the documented ``/tracks`` endpoint
        returns 403 for development-mode apps since Spotify's 2026 API
        changes, and the wrapper object is keyed ``item`` instead of
        ``track``. Playlists the user does not own still 403 — the caller
        should treat that as "browsable only via context playback".
        """
        tracks: list[Track] = []
        offset = 0
        while len(tracks) < 500:
            data = (
                self._request(
                    "GET", f"/playlists/{playlist_id}/items", params={"limit": limit, "offset": offset}
                )
                or {}
            )
            items = data.get("items", []) or []
            for wrapper in items:
                if wrapper is None or wrapper.get("is_local"):
                    continue
                obj = wrapper.get("item") or wrapper.get("track")
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") not in (None, "track") or obj.get("episode") is True:
                    continue
                track = parse_track(obj)
                if track is not None:
                    tracks.append(track)
            if data.get("next") is None or not items:
                break
            offset += limit
        return tracks[:500]

    def saved_tracks(self, limit: int = 50) -> list[Track]:
        tracks: list[Track] = []
        offset = 0
        while len(tracks) < 500:
            data = self._request("GET", "/me/tracks", params={"limit": limit, "offset": offset}) or {}
            items = data.get("items", []) or []
            for item in items:
                if item is None:
                    continue
                track = parse_track(item.get("track"))
                if track is not None:
                    tracks.append(track)
            if data.get("next") is None or not items:
                break
            offset += limit
        return tracks[:500]

    def save_track(self, id: str) -> None:  # noqa: A002 - matches Spotify's field name
        self._request("PUT", "/me/tracks", params={"ids": id})

    def unsave_track(self, id: str) -> None:  # noqa: A002
        self._request("DELETE", "/me/tracks", params={"ids": id})

    def is_saved(self, ids: list[str]) -> list[bool]:
        data = self._request("GET", "/me/tracks/contains", params={"ids": ",".join(ids)})
        return list(data) if data else []

    def recently_played(self, limit: int = 50) -> list[Track]:
        data = self._request("GET", "/me/player/recently-played", params={"limit": limit}) or {}
        items = data.get("items", []) or []
        tracks = (parse_track(i.get("track")) for i in items if i is not None)
        return [t for t in tracks if t is not None]

    def last_played(self) -> tuple[Track, float] | None:
        """Most recent history entry as ``(track, played_at_epoch)``.

        Note: Spotify only adds tracks to history once (mostly) finished, so
        an interrupted session's current track will not be here yet.
        """
        from datetime import datetime

        data = self._request("GET", "/me/player/recently-played", params={"limit": 1}) or {}
        for item in data.get("items", []) or []:
            track = parse_track((item or {}).get("track"))
            if track is None:
                continue
            try:
                stamp = datetime.fromisoformat(
                    item.get("played_at", "").replace("Z", "+00:00")).timestamp()
            except ValueError:
                stamp = 0.0
            return track, stamp
        return None

    def album_tracks(self, album_id: str) -> list[Track]:
        data = self._request("GET", f"/albums/{album_id}/tracks", params={"limit": 50}) or {}
        items = data.get("items", []) or []
        tracks = (parse_track(i) for i in items if i is not None)
        return [t for t in tracks if t is not None]

    def artist_top_tracks(self, artist_id: str) -> list[Track]:
        data = (
            self._request(
                "GET", f"/artists/{artist_id}/top-tracks", params={"market": "from_token"}
            )
            or {}
        )
        tracks = (parse_track(t) for t in data.get("tracks", []) or [])
        return [t for t in tracks if t is not None]


# -- parsing helpers ----------------------------------------------------------------


def parse_track(item: dict | None) -> Track | None:
    """Parse a Spotify track object; returns None for missing/local tracks."""
    if item is None or item.get("is_local"):
        return None
    album = item.get("album") or {}
    artists = tuple(a.get("name", "") for a in item.get("artists", []) or [])
    # Degraded dev-mode responses occasionally omit `uri`; the id is always
    # present, and a synthesized uri beats an empty one poisoning a payload.
    track_id = item.get("id", "")
    uri = item.get("uri") or (f"spotify:track:{track_id}" if track_id else "")
    return Track(
        id=track_id,
        uri=uri,
        name=item.get("name", ""),
        artists=artists,
        album=album.get("name", ""),
        duration_ms=item.get("duration_ms", 0),
        explicit=bool(item.get("explicit", False)),
    )


def parse_album(item: dict | None) -> Album | None:
    if item is None:
        return None
    artists = tuple(a.get("name", "") for a in item.get("artists", []) or [])
    return Album(
        id=item.get("id", ""),
        uri=item.get("uri", ""),
        name=item.get("name", ""),
        artists=artists,
        total_tracks=item.get("total_tracks", 0) or 0,
        release_date=item.get("release_date", "") or "",
    )


def parse_artist(item: dict | None) -> Artist | None:
    if item is None:
        return None
    return Artist(id=item.get("id", ""), uri=item.get("uri", ""), name=item.get("name", ""))


def parse_playlist(item: dict | None) -> Playlist | None:
    if item is None:
        return None
    owner = item.get("owner") or {}
    # 2026 dev-mode API renamed the "tracks" stub to "items"
    tracks = item.get("tracks") or item.get("items") or {}
    return Playlist(
        id=item.get("id", ""),
        uri=item.get("uri", ""),
        name=item.get("name", ""),
        owner=owner.get("display_name") or owner.get("id", ""),
        total_tracks=tracks.get("total", 0) or 0,
    )


def parse_device(item: dict | None) -> Device | None:
    if item is None:
        return None
    return Device(
        id=item.get("id", ""),
        name=item.get("name", ""),
        type=item.get("type", ""),
        is_active=bool(item.get("is_active", False)),
        volume_percent=item.get("volume_percent"),
    )


def parse_playback(json_data: dict) -> PlaybackState:
    """Parse the /me/player response. json_data["item"] may be None (nothing playing)."""
    device_data = json_data.get("device")
    context = json_data.get("context")
    return PlaybackState(
        is_playing=bool(json_data.get("is_playing", False)),
        progress_ms=json_data.get("progress_ms") or 0,
        track=parse_track(json_data.get("item")),
        device=parse_device(device_data),
        shuffle=bool(json_data.get("shuffle_state", False)),
        repeat=json_data.get("repeat_state", "off"),
        volume_percent=(device_data or {}).get("volume_percent"),
        context_uri=(context or {}).get("uri"),
    )


def _error_message(raw: bytes, fallback: str) -> str:
    """Extract a message from Spotify's {"error": {"status", "message"}} body."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return fallback or "unknown error"
    error = payload.get("error")
    if isinstance(error, dict):
        return error.get("message", fallback or "unknown error")
    if isinstance(error, str):
        return error
    return fallback or "unknown error"
