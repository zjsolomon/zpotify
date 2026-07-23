"""Tests for radio stations (zpotify's own recommendation engine).

Every Spotify call is faked; these run offline. The station has to survive
individual endpoints going 403 in the wild, so that is exercised explicitly.
"""

from __future__ import annotations

import random

import pytest

from zpotify.api import ApiError
from zpotify.models import Album, Artist, SearchResults, Track
from zpotify.radio import REFILL_SIZE, Station


def track(i: int, artist: str = "A", artist_id: str = "a1") -> Track:
    return Track(f"t{i}", f"spotify:track:t{i}", f"Song {i}", (artist,),
                 "Album", 200_000, artist_ids=(artist_id,))


SEED = track(0, "Seed Artist", "seed1")


class FakeAPI:
    """Records calls and returns canned data; every method can be made to 403."""

    def __init__(self, **canned) -> None:
        self.search_results: list[Track] = canned.get("search", [])
        self.top: list[Track] = canned.get("top", [])
        self.saved: list[Track] = canned.get("saved", [])
        self.recent: list[Track] = canned.get("recent", [])
        self.albums: list[Album] = canned.get("albums", [])
        self.album_track_map: dict[str, list[Track]] = canned.get("album_tracks", {})
        self.features: dict[str, dict] = canned.get("features", {})
        self.fail: set[str] = set(canned.get("fail", ()))
        self.searches: list[tuple[str, int]] = []
        self.opened_albums: list[str] = []
        self.library_reads = 0

    def _check(self, name: str) -> None:
        if name in self.fail:
            raise ApiError(403, "Forbidden")

    def search(self, q, types=(), limit=30, offset=0) -> SearchResults:
        self._check("search")
        self.searches.append((q, offset))
        return SearchResults(tracks=list(self.search_results), albums=[],
                             artists=[Artist("seed1", "spotify:artist:seed1", "Seed Artist")],
                             playlists=[])

    def top_tracks(self, time_range="medium_term", limit=50) -> list[Track]:
        self._check("top_tracks")
        self.library_reads += 1
        return list(self.top)

    def saved_tracks(self, limit=50) -> list[Track]:
        self._check("saved_tracks")
        self.library_reads += 1
        return list(self.saved)

    def recently_played(self, limit=50) -> list[Track]:
        self._check("recently_played")
        self.library_reads += 1
        return list(self.recent)

    def artist_albums(self, artist_id, limit=20) -> list[Album]:
        self._check("artist_albums")
        return list(self.albums)

    def album_tracks(self, album_id) -> list[Track]:
        self._check("album_tracks")
        self.opened_albums.append(album_id)
        return list(self.album_track_map.get(album_id, []))

    def audio_features(self, ids) -> dict[str, dict]:
        self._check("audio_features")
        return {i: self.features[i] for i in ids if i in self.features}


def station(api: FakeAPI, seed: Track = SEED) -> Station:
    return Station(api=api, seed=seed, rng=random.Random(0))


# -- pool construction -------------------------------------------------------------

def test_search_widens_the_net_by_seed_artist_name() -> None:
    api = FakeAPI(search=[track(i, f"Artist{i}", f"id{i}") for i in range(1, 12)])
    station(api).refill()
    assert api.searches[0][0] == "Seed Artist"


def test_seed_and_duplicates_never_enter_the_pool() -> None:
    dupes = [SEED, track(1, "B", "b1"), track(1, "B", "b1"), track(2, "C", "c1")]
    st = station(FakeAPI(search=dupes))
    fresh = st.refill()
    ids = [t.id for t in fresh]
    assert SEED.id not in ids
    assert len(ids) == len(set(ids))


def test_tracks_without_a_uri_are_dropped() -> None:
    broken = Track("t9", "", "No URI", ("B",), "Al", 1000, artist_ids=("b1",))
    st = station(FakeAPI(search=[broken, track(1, "B", "b1")]))
    assert "t9" not in [t.id for t in st.refill()]


def test_excluded_tracks_are_dropped_even_after_pooling() -> None:
    """A track queued after the pool was built must not be handed out again."""
    st = station(FakeAPI(search=[track(i, f"Artist{i}", f"id{i}") for i in range(1, 12)]))
    st.refill()
    pooled = st._pool[0]
    st.exclude([pooled])
    assert pooled.id not in [t.id for t in st.take(10)]


def test_take_pops_and_does_not_re_serve() -> None:
    st = station(FakeAPI(search=[track(i, f"Artist{i}", f"id{i}") for i in range(1, 12)]))
    st.refill()
    first = st.take(3)
    second = st.take(3)
    assert len(first) == 3
    assert not ({t.id for t in first} & {t.id for t in second})


# -- personal taste ----------------------------------------------------------------

def test_library_supplies_the_bulk_of_the_station() -> None:
    mine = [track(20 + i, f"Mine{i}", f"m{i}") for i in range(6)]
    api = FakeAPI(search=[], top=mine[:2], saved=mine[2:4], recent=mine[4:])
    ids = [t.id for t in station(api).refill()]
    assert all(t.id in ids for t in mine)


def test_library_is_read_once_and_reused_across_refills() -> None:
    """Refills draw the next-closest from a cached library, not a re-fetch."""
    api = FakeAPI(search=[], top=[track(20 + i, f"Mine{i}", f"m{i}") for i in range(40)])
    st = station(api)
    st.refill()
    after_first = api.library_reads
    st.refill()
    assert api.library_reads == after_first


def test_deep_cuts_come_from_the_seed_artists_albums() -> None:
    album = Album("al1", "spotify:album:al1", "Record", ("Seed Artist",))
    cut = track(30, "Seed Artist", "seed1")
    api = FakeAPI(search=[], albums=[album], album_tracks={"al1": [cut]})
    assert cut.id in [t.id for t in station(api).refill()]


# -- ranking and shaping -----------------------------------------------------------

def test_ranking_puts_the_closest_track_first() -> None:
    near, far = track(1, "Near", "n1"), track(2, "Far", "f1")
    api = FakeAPI(
        search=[far, near],
        features={
            SEED.id: {"energy": 0.5, "valence": 0.5, "danceability": 0.5,
                      "acousticness": 0.5, "tempo": 120},
            near.id: {"energy": 0.52, "valence": 0.5, "danceability": 0.5,
                      "acousticness": 0.5, "tempo": 121},
            far.id: {"energy": 0.99, "valence": 0.01, "danceability": 0.1,
                     "acousticness": 0.9, "tempo": 200},
        })
    assert [t.id for t in station(api).refill()][0] == near.id


def test_dead_audio_features_degrade_silently() -> None:
    """The endpoint is deprecated; losing it must cost ranking, not the station."""
    picks = [track(i, f"Artist{i}", f"id{i}") for i in range(1, 12)]
    st = station(FakeAPI(search=picks, fail={"audio_features"}))
    fresh = st.refill()
    assert len(fresh) >= 10          # still a full station
    assert st._features_dead         # and it stops retrying


def test_one_dead_source_does_not_kill_the_station() -> None:
    picks = [track(i, f"Artist{i}", f"id{i}") for i in range(1, 12)]
    api = FakeAPI(search=picks, fail={"top_tracks", "saved_tracks", "artist_albums"})
    assert len(station(api).refill()) >= 10


def test_artist_cap_and_no_consecutive_repeats() -> None:
    crowd = ([track(i, "Hog", "hog1") for i in range(1, 9)]
             + [track(20 + i, f"Other{i}", f"o{i}") for i in range(6)])
    fresh = station(FakeAPI(search=crowd)).refill()
    artists = [t.artists[0] for t in fresh]
    assert artists.count("Hog") <= 2
    assert all(a != b for a, b in zip(artists, artists[1:]))


def test_single_artist_pool_is_not_starved_by_the_cap() -> None:
    """The no-genre fallback is all one artist by design — keep it playable."""
    same = [track(i, "Seed Artist", "seed1") for i in range(1, 12)]
    assert len(station(FakeAPI(genres={}, search=same)).refill()) >= 10


def test_refill_never_exceeds_its_batch_size() -> None:
    lots = [track(i, f"Artist{i}", f"id{i}") for i in range(1, 80)]
    assert len(station(FakeAPI(search=lots)).refill()) <= REFILL_SIZE


# -- endlessness -------------------------------------------------------------------

def test_successive_refills_page_deeper_into_search() -> None:
    api = FakeAPI(search=[track(i, f"Artist{i}", f"id{i}") for i in range(1, 12)])
    st = station(api)
    st.refill()
    st.refill()
    st.refill()
    offsets = [off for _, off in api.searches]
    assert offsets == sorted(offsets) and offsets[-1] > offsets[0]


def test_successive_refills_open_different_albums() -> None:
    albums = [Album(f"al{i}", f"spotify:album:al{i}", f"Rec{i}", ("Seed Artist",))
              for i in range(6)]
    api = FakeAPI(search=[], albums=albums,
                  album_tracks={a.id: [track(100 + i, "Seed Artist", "seed1")]
                                for i, a in enumerate(albums)})
    st = station(api)
    st.refill()
    first = set(api.opened_albums)
    api.opened_albums.clear()
    st.refill()
    assert first and not (first & set(api.opened_albums))


def test_label_names_the_seed_artist() -> None:
    assert station(FakeAPI()).label == "Seed Artist"


def test_seed_without_artist_ids_is_recovered_by_search() -> None:
    """Sessions saved before radio existed carry no artist ids."""
    legacy = Track("t0", "spotify:track:t0", "Song", ("Seed Artist",), "Al", 1000)
    album = Album("al1", "spotify:album:al1", "Record", ("Seed Artist",))
    cut = track(30, "Seed Artist", "seed1")
    api = FakeAPI(search=[], albums=[album], album_tracks={"al1": [cut]})
    # deep cuts require resolving the artist id, which only search can supply
    assert cut.id in [t.id for t in station(api, seed=legacy).refill()]


@pytest.mark.parametrize("dead", ["search", "audio_features", "album_tracks"])
def test_station_survives_core_endpoint_failure(dead: str) -> None:
    st = station(FakeAPI(search=[track(1, "B", "b1")], fail={dead}))
    st.refill()  # must not raise
