"""Radio stations — zpotify's own recommendation engine.

Spotify gives development-mode apps nothing to build radio out of:
`/recommendations` 404s, and `related-artists`, `artists/{id}/top-tracks`, the
batch `/artists?ids=` and all of `/browse/*` return 403. `/artists/{id}` does
answer, but with a stripped object carrying no `genres` and no `popularity`
(album `genres` come back empty too) — so there is no way to learn what a seed
track *is*, genre-wise, and no catalogue-wide similarity signal to query.

What remains is enough. Audio features still resolve, which gives a real
acoustic fingerprint per track, and the user's own listening history is fully
readable. So a station is: candidates drawn from the listener's library and the
seed artist's catalogue, ranked by how close they sound to the seed. Less a
global discovery engine than "the music you have, sorted to sound like this" —
but honest, and it degrades rather than breaks when endpoints disappear.

A `Station` is a lazily-refilling pool seeded from one track. Each refill takes
the next-closest candidates it has not offered yet, rotating albums and search
pages so a station that runs for hours keeps finding new material.

Everything in here blocks on network I/O and must be driven from a worker
thread (see `App.workers`), never the render loop.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from zpotify.api import ApiError, SpotifyAPI
from zpotify.models import Track

REFILL_SIZE = 24        # tracks added per refill
REFILL_BELOW = 8        # top up once the queue drops under this
_MAX_PER_ARTIST = 2     # diversity cap within one refill
_ALBUMS_PER_REFILL = 3  # seed-artist albums opened per refill
_FEATURE_BATCH = 100    # ids per /audio-features call


@dataclass
class Station:
    """An endless track pool seeded from one song."""

    api: SpotifyAPI
    seed: Track
    rng: random.Random = field(default_factory=random.Random)

    _pool: list[Track] = field(default_factory=list, init=False)
    # Everything ever generated, so refills stop re-proposing it...
    _seen: set[str] = field(default_factory=set, init=False)
    # ...and what is already queued or played, which must also be dropped from
    # a pool that was built before it got there.
    _excluded: set[str] = field(default_factory=set, init=False)
    _library: list[Track] | None = field(default=None, init=False)
    _features: dict[str, dict] = field(default_factory=dict, init=False)
    _seed_features: dict | None = field(default=None, init=False)
    _features_dead: bool = field(default=False, init=False)
    _round: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._seen.add(self.seed.id)

    # -- public surface ----------------------------------------------------------

    @property
    def label(self) -> str:
        """How the station names itself in the UI."""
        return self.seed.artists[0] if self.seed.artists else self.seed.name

    @property
    def pending(self) -> int:
        return len(self._pool)

    def exclude(self, tracks: list[Track]) -> None:
        """Mark tracks as already queued/played so the station stops offering them."""
        ids = {t.id for t in tracks if t.id}
        self._seen.update(ids)
        self._excluded.update(ids)

    def take(self, n: int) -> list[Track]:
        """Pop up to ``n`` tracks, skipping anything excluded since pooling."""
        out: list[Track] = []
        keep: list[Track] = []
        for track in self._pool:
            if track.id in self._excluded:
                continue
            if len(out) < n:
                out.append(track)
            else:
                keep.append(track)
        self._pool = keep
        self._excluded.update(t.id for t in out)
        return out

    def refill(self) -> list[Track]:
        """Blocking: fetch one more batch and return what was added.

        Every source is individually guarded — a single endpoint going 403 in
        the wild degrades the mix rather than killing the station.
        """
        candidates: list[Track] = []
        for source in (self._from_library, self._from_deep_cuts, self._from_search):
            try:
                candidates.extend(source())
            except ApiError:
                continue

        fresh = self._dedupe(candidates)
        fresh = self._rank(fresh)
        fresh = self._diversify(fresh)
        self._seen.update(t.id for t in fresh)
        self._pool.extend(fresh)
        self._round += 1
        return fresh

    # -- sources -----------------------------------------------------------------

    def _seed_artist_id(self) -> str:
        if self.seed.artist_ids:
            return self.seed.artist_ids[0]
        # Sessions written before radio existed carry no artist ids; recover
        # one by name so restored sessions can still start a station.
        name = self.seed.artists[0] if self.seed.artists else ""
        if not name:
            return ""
        try:
            found = self.api.search(name, types=("artist", "track"), limit=1)
        except ApiError:
            return ""
        return found.artists[0].id if found.artists else ""

    def _from_library(self) -> list[Track]:
        """Everything the listener already has, fetched once and reused.

        This is the station's main body: ranking picks whichever of these
        sound closest to the seed, so each refill draws the next-nearest
        tracks rather than re-reading the library.
        """
        if self._library is None:
            tracks: list[Track] = []
            for window in ("short_term", "medium_term", "long_term"):
                try:
                    tracks.extend(self.api.top_tracks(window, limit=50))
                except ApiError:
                    continue
            for fetch in (lambda: self.api.saved_tracks(limit=50),
                          lambda: self.api.recently_played(limit=50)):
                try:
                    tracks.extend(fetch())
                except ApiError:
                    continue
            self._library = tracks
        return list(self._library)

    def _from_deep_cuts(self) -> list[Track]:
        """Album tracks by the seed artist — the 'more of this' half.

        A different slice of the discography opens on each refill, so a long
        station works through the catalogue instead of re-reading one record.
        """
        artist_id = self._seed_artist_id()
        if not artist_id:
            return []
        albums = self.api.artist_albums(artist_id, limit=20)
        if not albums:
            return []
        start = (self._round * _ALBUMS_PER_REFILL) % len(albums)
        out: list[Track] = []
        for album in (albums * 2)[start:start + _ALBUMS_PER_REFILL]:
            try:
                out.extend(self.api.album_tracks(album.id))
            except ApiError:
                continue
        return out

    def _from_search(self) -> list[Track]:
        """Seed-artist search — collaborations and features the albums miss.

        Spotify exposes no similarity search to development-mode apps (see the
        module docstring), so this widens the net by name only.
        """
        found = self.api.search(self.label, types=("track", "artist"),
                                limit=20, offset=self._round * 10)
        return list(found.tracks)

    # -- shaping -----------------------------------------------------------------

    def _dedupe(self, tracks: list[Track]) -> list[Track]:
        seen = set(self._seen)
        out: list[Track] = []
        for track in tracks:
            if not track.id or not track.uri or track.id in seen:
                continue
            seen.add(track.id)
            out.append(track)
        return out

    def _rank(self, tracks: list[Track]) -> list[Track]:
        """Sort by audio-feature distance from the seed, closest first.

        This is the station's similarity signal — with no recommendations or
        related-artists endpoint, it is the only one left. Spotify has
        announced its removal too; when it goes, stations fall back to a
        shuffle of the same candidates rather than failing.
        """
        if not tracks:
            return tracks
        if self._features_dead or not self._load_features(tracks):
            shuffled = list(tracks)
            self.rng.shuffle(shuffled)
            return shuffled

        seed = self._seed_features or {}

        def distance(track: Track) -> float:
            other = self._features.get(track.id)
            if not other:
                return 1e6  # unknown: sort to the back, but never dropped
            return _feature_distance(seed, other)

        return sorted(tracks, key=distance)

    def _load_features(self, tracks: list[Track]) -> bool:
        """Fill the feature cache for `tracks`; False if ranking is unavailable."""
        try:
            if self._seed_features is None:
                self._seed_features = (
                    self.api.audio_features([self.seed.id]).get(self.seed.id) or {})
            if not self._seed_features:
                self._features_dead = True
                return False
            missing = [t.id for t in tracks if t.id not in self._features]
            for start in range(0, len(missing), _FEATURE_BATCH):
                self._features.update(
                    self.api.audio_features(missing[start:start + _FEATURE_BATCH]))
        except ApiError:
            self._features_dead = True
            return False
        return True

    def _diversify(self, tracks: list[Track]) -> list[Track]:
        """Cap tracks per artist and never play the same artist twice in a row.

        Skipped when the candidates come from fewer than three artists — that
        is the no-genre fallback, where everything is deliberately by the seed
        artist and spreading it out would just starve the station.
        """
        buckets: dict[str, list[Track]] = {}
        for track in tracks:
            buckets.setdefault(_artist_key(track), []).append(track)
        if len(buckets) < 3:
            return tracks[:REFILL_SIZE]

        for held in buckets.values():
            del held[_MAX_PER_ARTIST:]
        out: list[Track] = []
        last: str | None = None
        while len(out) < REFILL_SIZE:
            # Draw from the fullest bucket that isn't the artist just used, so
            # heavily represented artists get spaced out rather than clustered.
            options = [k for k, v in buckets.items() if v and k != last]
            if not options:
                break
            pick = max(options, key=lambda k: len(buckets[k]))
            out.append(buckets[pick].pop(0))
            last = pick
        return out


def _artist_key(track: Track) -> str:
    return track.artists[0] if track.artists else track.id


_FEATURE_KEYS = ("energy", "valence", "danceability", "acousticness")


def _feature_distance(a: dict, b: dict) -> float:
    """Euclidean distance over normalized audio features (tempo scaled to 0-1)."""
    total = 0.0
    for key in _FEATURE_KEYS:
        try:
            total += (float(a.get(key, 0.0)) - float(b.get(key, 0.0))) ** 2
        except (TypeError, ValueError):
            continue
    try:
        tempo = (float(a.get("tempo", 0.0)) - float(b.get("tempo", 0.0))) / 250.0
        total += tempo ** 2
    except (TypeError, ValueError):
        pass
    return total ** 0.5
