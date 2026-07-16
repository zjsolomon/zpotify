"""Plain data models shared between the API layer and the UI.

Parsing from Spotify API JSON lives in api.py; these stay dumb.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Track:
    id: str
    uri: str
    name: str
    artists: tuple[str, ...]
    album: str
    duration_ms: int
    explicit: bool = False

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)

    def to_dict(self) -> dict:
        return {"id": self.id, "uri": self.uri, "name": self.name,
                "artists": list(self.artists), "album": self.album,
                "duration_ms": self.duration_ms, "explicit": self.explicit}

    @classmethod
    def from_dict(cls, data: dict) -> "Track":
        return cls(id=data.get("id", ""), uri=data.get("uri", ""),
                   name=data.get("name", ""),
                   artists=tuple(data.get("artists") or ()),
                   album=data.get("album", ""),
                   duration_ms=int(data.get("duration_ms", 0)),
                   explicit=bool(data.get("explicit", False)))


@dataclass(frozen=True)
class Playlist:
    id: str
    uri: str
    name: str
    owner: str
    total_tracks: int


@dataclass(frozen=True)
class Album:
    id: str
    uri: str
    name: str
    artists: tuple[str, ...]
    total_tracks: int = 0
    release_date: str = ""


@dataclass(frozen=True)
class Artist:
    id: str
    uri: str
    name: str


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    type: str
    is_active: bool
    volume_percent: int | None = None


@dataclass
class PlaybackState:
    is_playing: bool = False
    progress_ms: int = 0
    track: Track | None = None
    device: Device | None = None
    shuffle: bool = False
    repeat: str = "off"  # off | context | track
    volume_percent: int | None = None
    context_uri: str | None = None


@dataclass
class SearchResults:
    tracks: list[Track] = field(default_factory=list)
    albums: list[Album] = field(default_factory=list)
    artists: list[Artist] = field(default_factory=list)
    playlists: list[Playlist] = field(default_factory=list)
