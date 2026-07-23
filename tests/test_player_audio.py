"""Tests for the audio ring buffer: backpressure, volume math, reader thread."""

from __future__ import annotations

import io
import threading
import time

import numpy as np

from zpotify.player.audio import AudioEngine, apply_volume


def _frames(n: int, value: int = 1000, channels: int = 2) -> np.ndarray:
    return np.full((n, channels), value, dtype=np.int16)


def _drain(eng: AudioEngine, n: int) -> np.ndarray:
    out = np.empty((n, eng.channels), dtype=np.int16)
    got = eng._read(n, out)
    return out[:got]


def test_apply_volume_math() -> None:
    frames = np.full((4, 2), 1000, dtype=np.int16)
    assert np.array_equal(apply_volume(frames, 1.0), frames)  # unity passthrough
    half = apply_volume(frames, 0.5)
    assert half.dtype == np.int16
    # 0.5 -> gain 128 -> (1000*128)>>8 == 500
    assert np.all(half == 500)
    assert np.all(apply_volume(frames, 0.0) == 0)


def test_write_read_roundtrip() -> None:
    eng = AudioEngine()
    eng._write(_frames(1024, value=2048))
    out = _drain(eng, 1024)
    assert out.shape == (1024, 2)
    assert np.all(out == 2048)
    assert eng._buffered == 0


def test_latest_reflects_played_audio_only() -> None:
    eng = AudioEngine()
    eng._write(_frames(1024, value=2048))
    # nothing consumed yet -> the visualizer tap is silent
    assert np.all(eng.latest(256) == 0.0)
    _drain(eng, 1024)
    out = eng.latest(1024)
    assert out.shape == (1024,)
    assert np.allclose(out, 2048 / 32768.0, atol=1e-4)


def test_latest_zero_pads_when_underfilled() -> None:
    eng = AudioEngine()
    eng._write(_frames(100, value=3000))
    _drain(eng, 100)
    out = eng.latest(500)
    assert np.all(out[:400] == 0.0)  # left-padded
    assert np.allclose(out[400:], 3000 / 32768.0, atol=1e-4)


def test_write_blocks_when_full_until_read_frees_space() -> None:
    eng = AudioEngine()
    cap = eng._fill_limit
    eng._write(_frames(cap, value=100))  # exactly fills: no block
    assert eng._buffered == cap

    done = threading.Event()
    writer = threading.Thread(
        target=lambda: (eng._write(_frames(1000, value=777)), done.set()),
        daemon=True,
    )
    writer.start()
    time.sleep(0.15)
    assert not done.is_set()  # backpressure: writer is stuck on a full ring
    assert eng._buffered == cap

    _drain(eng, 1000)  # audio callback consumes -> space frees
    assert done.wait(timeout=2.0)
    assert eng._buffered == cap  # refilled with the blocked writer's frames


def test_no_frames_are_dropped_under_backpressure() -> None:
    eng = AudioEngine()
    total = eng._fill_limit * 3  # much more than fits at once
    payload = np.arange(total, dtype=np.int16).reshape(-1, 1)
    payload = np.repeat(payload, 2, axis=1)

    writer = threading.Thread(target=lambda: eng._write(payload), daemon=True)
    writer.start()
    received = []
    deadline = time.time() + 5.0
    while sum(len(r) for r in received) < total and time.time() < deadline:
        chunk = _drain(eng, 4096)
        if len(chunk):
            received.append(chunk.copy())
        else:
            time.sleep(0.005)
    writer.join(timeout=2.0)
    got = np.concatenate(received)
    assert len(got) == total
    assert np.array_equal(got, payload)  # in order, nothing dropped


def test_flush_empties_ring_and_unblocks_writer() -> None:
    eng = AudioEngine()
    eng._write(_frames(eng._fill_limit, value=100))
    done = threading.Event()
    writer = threading.Thread(
        target=lambda: (eng._write(_frames(64, value=9)), done.set()), daemon=True,
    )
    writer.start()
    time.sleep(0.05)
    eng.flush()
    assert done.wait(timeout=2.0)
    assert eng._buffered == 64  # only the post-flush write remains
    assert np.all(eng.latest(16) == 0.0)  # tap cleared too


def test_prebuffer_gate_in_callback() -> None:
    eng = AudioEngine(blocksize=64)
    out = np.empty((64, 2), dtype=np.int16)
    eng._write(_frames(128, value=5000))  # below the prime threshold
    eng._callback(out, 64, None, None)
    assert np.all(out == 0)  # gated: silence while prebuffering
    assert eng.last_error is None

    eng._write(_frames(eng._prime_frames, value=5000))
    eng._callback(out, 64, None, None)
    assert np.all(out != 0)  # primed: audio flows
    assert eng.last_error is None


def test_read_pops_and_pads() -> None:
    eng = AudioEngine()
    eng._write(_frames(50, value=1234))
    scratch = np.empty((100, 2), dtype=np.int16)
    got = eng._read(100, scratch)
    assert got == 50
    assert np.all(scratch[:50] == 1234)


def test_attach_drains_stream_to_eof() -> None:
    eng = AudioEngine()
    frames = _frames(4096, value=555)
    stream = io.BytesIO(frames.tobytes())
    eng.attach(stream)
    reader = eng._reader
    assert reader is not None
    reader.join(timeout=2.0)
    assert not reader.is_alive()  # exited cleanly on EOF
    assert eng._buffered == 4096
    out = _drain(eng, 4096)
    assert np.all(out == 555)


def test_attach_can_be_called_again() -> None:
    eng = AudioEngine()
    eng.attach(io.BytesIO(_frames(1024).tobytes()))
    if eng._reader is not None:
        eng._reader.join(timeout=2.0)
    _drain(eng, 1024)
    eng.attach(io.BytesIO(_frames(2048, value=999).tobytes()))
    reader = eng._reader
    assert reader is not None
    reader.join(timeout=2.0)
    assert not reader.is_alive()
    out = _drain(eng, 2048)
    assert np.all(out == 999)


def test_attach_carries_partial_frames_across_pipe_reads() -> None:
    """Raw pipe reads can split a frame anywhere; the remainder must carry —
    dropping it would shift every later sample's channel alignment."""
    import os

    eng = AudioEngine()
    frames = np.arange(4096 * 2, dtype=np.int16).reshape(-1, 2)
    payload = frames.tobytes()
    rfd, wfd = os.pipe()
    eng.attach(os.fdopen(rfd, "rb", buffering=0))
    # deliberately misaligned chunk sizes (never a multiple of 4)
    sizes = [4093, 7, 1, 4097, 2, 4096 * 4]
    offset = 0
    for size in sizes:
        os.write(wfd, payload[offset:offset + min(size, len(payload) - offset)])
        offset += size
        if offset >= len(payload):
            break
        time.sleep(0.02)  # let the reader consume the odd tail
    os.close(wfd)
    reader = eng._reader
    assert reader is not None
    reader.join(timeout=2.0)
    assert not reader.is_alive()
    assert eng._buffered == 4096
    out = _drain(eng, 4096)
    assert np.array_equal(out, frames)  # exact samples, no shift, no loss


def test_attach_pipe_reader_exits_on_eof() -> None:
    import os

    eng = AudioEngine()
    rfd, wfd = os.pipe()
    eng.attach(os.fdopen(rfd, "rb", buffering=0))
    os.write(wfd, _frames(64, value=7).tobytes())
    os.close(wfd)
    reader = eng._reader
    assert reader is not None
    reader.join(timeout=2.0)
    assert not reader.is_alive()
    assert eng._reader_fd is None
    assert np.all(_drain(eng, 64) == 7)


def test_fade_envelope_ramps_down_and_up() -> None:
    eng = AudioEngine(blocksize=441)  # 100 blocks/sec
    # exactly fill the ring: enough for the 20 blocks below, and _write must
    # not block (it would deadlock the test — there's no consumer thread)
    eng._write(_frames(eng._fill_limit, value=10000))
    out = np.empty((441, 2), dtype=np.int16)

    eng._callback(out, 441, None, None)  # primes, env=1
    assert np.all(out == 8007)  # volume 0.8 -> gain 205 -> (10000*205)>>8

    eng.fade_to(0.0, 0.1)  # 10 blocks to silence
    levels = []
    for _ in range(12):
        eng._callback(out, 441, None, None)
        levels.append(abs(int(out[0][0])))
    assert levels[0] < 8007            # ramping immediately
    assert levels[-1] == 0             # fully silent
    assert all(a >= b for a, b in zip(levels, levels[1:]))  # monotonic down

    eng.fade_to(1.0, 0.05)  # 5 blocks back up
    for _ in range(7):
        eng._callback(out, 441, None, None)
    assert np.all(out == 8007)
    assert eng.env == 1.0


def test_set_env_is_instant_and_flush_keeps_env() -> None:
    eng = AudioEngine()
    eng.set_env(0.0)
    assert eng.env == 0.0
    eng.fade_to(1.0, 0.0)  # zero seconds -> instant
    assert eng.env == 1.0


# -- crossfade -----------------------------------------------------------------

def test_crossfade_mixes_tail_into_head() -> None:
    sr = 44100
    eng = AudioEngine(samplerate=sr)
    eng.set_crossfade(0.1)               # X = 4410 frames
    x = eng._xfade_frames
    eng._write(_frames(8000, value=10000))   # track 1
    eng.note_end_of_track()                  # boundary at frame 8000
    eng._write(_frames(x + 2000, value=-10000))  # track 2 head + spare

    # before the window: pure track 1
    out = _drain(eng, 8000 - x)
    assert np.all(out == 10000)
    # inside the window: mixed samples move from track1 toward track2
    mix = _drain(eng, x)
    assert len(mix) == x
    assert mix[0][0] > 8000                # start ~ track 1
    assert mix[-1][0] < -8000              # end ~ track 2
    mid = mix[x // 2][0]
    assert -3000 < mid < 3000              # halfway: roughly equal blend
    # equal-power halfway point: each source at ~0.707 gain, so the mix of
    # opposite-sign equals cancels near zero while power stays constant
    quarter = mix[x // 4][0]
    assert quarter > 2500                  # cos(22.5°) − sin(22.5°) ≈ 0.54
    # after the mix: cursor jumped past the head region already played
    after = _drain(eng, 2000)
    assert len(after) == 2000 and np.all(after == -10000)


def test_crossfade_off_means_no_mixing() -> None:
    eng = AudioEngine()
    eng.set_crossfade(0.0)
    eng._write(_frames(1000, value=7))
    eng.note_end_of_track()                # ignored when off
    eng._write(_frames(1000, value=9))
    out = _drain(eng, 2000)
    assert np.all(out[:1000] == 7) and np.all(out[1000:] == 9)


def test_crossfade_without_head_plays_tail_unmixed() -> None:
    """The mix must never start against an empty head: the tail plays plainly
    to the boundary, then the (late) next track starts as a hard transition."""
    eng = AudioEngine()
    eng.set_crossfade(0.1)
    x = eng._xfade_frames
    eng._write(_frames(x, value=10000))    # exactly one window of tail
    eng.note_end_of_track()                # no head has arrived at all
    out = _drain(eng, x)
    assert len(out) == x
    assert np.all(out == 10000)            # NOT faded against silence
    eng._write(_frames(500, value=-10000))  # head arrives late
    late = _drain(eng, 500)
    assert np.all(late == -10000)          # plain start, no mixing


def test_crossfade_shrinks_to_available_head() -> None:
    """Head buffered for only half the window: the mix defers while playing
    tail, and the overlap becomes what the stream can sustain."""
    eng = AudioEngine()
    eng.set_crossfade(0.1)
    x = eng._xfade_frames
    eng._write(_frames(2 * x, value=10000))
    eng.note_end_of_track()                # boundary at 2x
    eng._write(_frames(x // 2, value=-10000))  # only half a window of head

    out = _drain(eng, 2 * x)               # drain everything readable
    n_pure = int((out[:, 0] == 10000).sum())
    assert x <= n_pure < 2 * x             # mix deferred past the naive window
    mixed = out[n_pure:]
    # the mix's first sample carries gain 0 (== pure tail), hence the ±1
    assert abs(len(mixed) - x // 2) <= 1   # overlap shrank to available head
    assert mixed[-1][0] < -8000            # and does reach the next track


def test_set_crossfade_reserves_double_the_overlap() -> None:
    eng = AudioEngine()
    base = eng._fill_limit
    eng.set_crossfade(5.0)
    assert eng._fill_limit >= 2 * 5 * eng.samplerate  # tail + head
    eng.set_crossfade(0.0)
    assert eng._fill_limit == base


def test_crossed_ms_counts_audible_position_of_new_track() -> None:
    sr = 44100
    eng = AudioEngine(samplerate=sr)
    eng.set_crossfade(0.1)
    x = eng._xfade_frames
    assert eng.crossed_ms is None          # no boundary yet
    eng._write(_frames(2 * x, value=10))
    eng.note_end_of_track()
    eng._write(_frames(2 * x, value=20))
    _drain(eng, 2 * x - x)                 # up to the mix window
    _drain(eng, x // 2)                    # halfway through the mix
    assert abs(eng.crossed_ms - int(x // 2 * 1000 / sr)) <= 2
    _drain(eng, x // 2)                    # finish the mix
    _drain(eng, 1000)                      # 1000 frames into plain track 2
    expected = int((x + 1000) * 1000 / sr)
    assert abs(eng.crossed_ms - expected) <= 2
    eng.flush()
    assert eng.crossed_ms is None          # seek/skip resets the clock


def test_note_end_of_track_counts_write_backlog_and_pipe() -> None:
    """The boundary must count every frame the outgoing track produced:
    ringed frames, frames held by a blocked _write, and unread pipe bytes."""
    import os

    eng = AudioEngine(buffer_seconds=0.01)  # tiny fill: blocksize*4 = 4096
    eng.set_crossfade(0.1)
    fill = eng._fill_limit
    extra = 3000
    rfd, wfd = os.pipe()
    eng.attach(os.fdopen(rfd, "rb", buffering=0))
    payload = _frames(fill + extra, value=42).tobytes()
    writer = threading.Thread(target=lambda: os.write(wfd, payload), daemon=True)
    writer.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and eng._buffered < fill:
        time.sleep(0.005)
    assert eng._buffered == fill           # reader blocked at the fill limit
    time.sleep(0.05)                       # let pipe/backlog settle
    eng.note_end_of_track()
    assert eng._boundaries[0] == fill + extra
    eng.flush()                            # unblock the reader
    os.close(wfd)


def test_note_end_of_track_ignored_when_off_or_empty() -> None:
    eng = AudioEngine()
    eng._write(_frames(100))
    eng.note_end_of_track()                # crossfade off
    assert not eng.transition_pending
    eng.set_crossfade(0.1)
    _drain(eng, 100)
    eng.note_end_of_track()                # nothing buffered
    assert not eng.transition_pending


def test_note_end_of_track_latency_subtracts_but_clamps() -> None:
    eng = AudioEngine()
    eng.set_crossfade(0.1)
    eng._write(_frames(5000))
    eng.note_end_of_track(latency_seconds=0.05)   # 2205 frames stale
    assert eng._boundaries[-1] == 5000 - 2205

    eng2 = AudioEngine()
    eng2.set_crossfade(0.1)
    eng2._write(_frames(5000))
    _drain(eng2, 4000)
    eng2.note_end_of_track(latency_seconds=99.0)  # absurd: clamped, floored
    assert eng2._boundaries[-1] == eng2._readpos  # never behind the playhead


def test_flush_clears_boundaries_and_mix_state() -> None:
    eng = AudioEngine()
    eng.set_crossfade(0.1)
    eng._write(_frames(1000))
    eng.note_end_of_track()
    assert eng.transition_pending
    eng.flush()
    assert not eng.transition_pending
    assert eng.crossed_ms is None


def test_pause_mid_mix_resumes_mix_in_place() -> None:
    eng = AudioEngine(blocksize=441)
    eng.set_crossfade(0.05)                # X = 2205 frames = 5 blocks
    x = eng._xfade_frames
    eng._write(_frames(x, value=10000))
    eng.note_end_of_track()
    eng._write(_frames(2 * x, value=-10000))
    out = np.empty((441, 2), dtype=np.int16)
    eng._callback(out, 441, None, None)    # first mix block
    first = out[0][0]
    eng.hold(0.0)
    eng._callback(out, 441, None, None)
    assert np.all(out == 0)                # held: silence, mix state frozen
    eng.release(0.0)
    eng._callback(out, 441, None, None)    # mix continues where it left off
    assert out[0][0] < first               # gain kept ramping, no restart
    eng = AudioEngine()
    base = eng.latency
    assert abs(base - 0.37) < 1e-6  # empty ring: just the pipe estimate
    eng._write(_frames(44100 // 10))  # +0.1s
    assert abs(eng.latency - (0.47)) < 0.005



# -- hold / release (instant local pause) ----------------------------------------

def test_hold_gates_output_and_release_resumes_in_place() -> None:
    eng = AudioEngine(blocksize=64)
    out = np.empty((64, 2), dtype=np.int16)
    eng._write(_frames(eng._prime_frames + 640, value=1000))
    eng._callback(out, 64, None, None)
    assert np.all(out != 0)
    buffered_before = eng._buffered

    eng.hold(0.0)                          # instant hold
    assert eng.held
    eng._callback(out, 64, None, None)
    assert np.all(out == 0)
    assert eng._buffered == buffered_before  # nothing consumed while held

    eng.release(0.0)
    assert not eng.held
    eng._callback(out, 64, None, None)
    assert np.all(out != 0)                # resumes from the same spot


def test_hold_with_fade_engages_after_ramp() -> None:
    eng = AudioEngine(blocksize=441)
    out = np.empty((441, 2), dtype=np.int16)
    eng._write(_frames(eng._fill_limit, value=10000))
    eng._callback(out, 441, None, None)    # primed, playing
    eng.hold(0.05)                         # ~5 blocks of fade first
    assert eng.held                        # pending counts as held
    for _ in range(8):
        eng._callback(out, 441, None, None)
    assert np.all(out == 0)                # fully gated after the ramp
    assert eng._held
