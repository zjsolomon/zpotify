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
    cap = eng._capacity
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
    total = eng._capacity * 3  # much more than fits at once
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
    eng._write(_frames(eng._capacity, value=100))
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


def test_fade_envelope_ramps_down_and_up() -> None:
    eng = AudioEngine(blocksize=441)  # 100 blocks/sec
    # exactly fill the ring: enough for the 20 blocks below, and _write must
    # not block (it would deadlock the test — there's no consumer thread)
    eng._write(_frames(eng._capacity, value=10000))
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


def test_boundary_fade_fires_on_prime_transition() -> None:
    eng = AudioEngine(blocksize=441)
    out = np.empty((441, 2), dtype=np.int16)
    eng.arm_boundary_fade(0.1)  # 10 blocks to full volume
    assert eng.boundary_fade_armed

    # below the prime threshold: gated silence, fade still armed, env untouched
    eng._write(_frames(128, value=10000))
    eng._callback(out, 441, None, None)
    assert np.all(out == 0) and eng.boundary_fade_armed and eng.env == 1.0

    # prime: fade consumes, output ramps from silence
    eng._write(_frames(eng._capacity - 128, value=10000))
    eng._callback(out, 441, None, None)
    assert not eng.boundary_fade_armed
    first = abs(int(out[-1][0]))
    assert first < 2000  # started near silence
    for _ in range(12):
        eng._callback(out, 441, None, None)
    assert np.all(out == 8007)  # fully faded in (volume 0.8)
    assert eng.env == 1.0


def test_disarm_boundary_fade() -> None:
    eng = AudioEngine(blocksize=64)
    eng.arm_boundary_fade(1.0)
    eng.disarm_boundary_fade()
    out = np.empty((64, 2), dtype=np.int16)
    eng._write(_frames(eng._prime_frames + 64, value=10000))
    eng._callback(out, 64, None, None)
    assert np.all(out == 8007)  # no fade: full volume immediately


def test_latency_reflects_buffered_frames() -> None:
    eng = AudioEngine()
    base = eng.latency
    assert abs(base - 0.37) < 1e-6  # empty ring: just the pipe estimate
    eng._write(_frames(44100 // 10))  # +0.1s
    assert abs(eng.latency - (0.47)) < 0.005


def test_boundary_fade_deadline_fires_without_underrun() -> None:
    eng = AudioEngine(blocksize=441)
    out = np.empty((441, 2), dtype=np.int16)
    eng._write(_frames(eng._capacity, value=10000))
    eng._callback(out, 441, None, None)       # primes normally, env=1
    eng.set_env(0.0)                          # as after a completed fade-out
    eng.arm_boundary_fade(0.1, timeout=0.0)   # deadline already passed
    eng._callback(out, 441, None, None)       # no underrun ever happens
    assert not eng.boundary_fade_armed        # fallback consumed it
    for _ in range(12):
        eng._callback(out, 441, None, None)
    assert np.all(out == 8007)                # sound came back
    assert eng.env == 1.0
