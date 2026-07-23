"""Tests for librespot player-event parsing (the --onevent hook lines)."""

from __future__ import annotations

from zpotify.player.librespot import _parse_player_event


def test_end_of_track_line_parses() -> None:
    event = _parse_player_event("end_of_track 6bIkBV2Wpw08A8KDJA7psE 228821")
    assert event is not None
    assert event.kind == "end_of_track"
    assert event.data["track_id"] == "6bIkBV2Wpw08A8KDJA7psE"
    assert event.data["position_ms"] == 228821


def test_transport_events_map_to_existing_kinds() -> None:
    assert _parse_player_event("playing tid 0").kind == "playing"
    assert _parse_player_event("paused tid 1000").kind == "paused"
    assert _parse_player_event("stopped tid").kind == "stopped"


def test_uninteresting_and_malformed_lines_ignored() -> None:
    assert _parse_player_event("preloading tid") is None
    assert _parse_player_event("session_connected") is None
    assert _parse_player_event("") is None
    assert _parse_player_event("   ") is None
    # missing fields degrade, never crash
    bare = _parse_player_event("end_of_track")
    assert bare is not None and "track_id" not in bare.data
    weird = _parse_player_event("playing tid not-a-number")
    assert weird is not None and "position_ms" not in weird.data
