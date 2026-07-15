"""Tests for the audio ring buffer, volume math, and reader thread."""

from __future__ import annotations

import io

import numpy as np

from zpotify.player.audio import AudioEngine, apply_volume


def _frames(n: int, value: int = 1000, channels: int = 2) -> np.ndarray:
    return np.full((n, channels), value, dtype=np.int16)


def test_apply_volume_math() -> None:
    frames = np.full((4, 2), 1000, dtype=np.int16)
    assert np.array_equal(apply_volume(frames, 1.0), frames)  # unity passthrough
    half = apply_volume(frames, 0.5)
    assert half.dtype == np.int16
    # 0.5 -> gain 128 -> (1000*128)>>8 == 500
    assert np.all(half == 500)
    assert np.all(apply_volume(frames, 0.0) == 0)


def test_latest_roundtrip() -> None:
    eng = AudioEngine()
    eng._write(_frames(1024, value=2048))
    out = eng.latest(1024)
    assert out.shape == (1024,)
    # mono mean of both channels == 2048 -> /32768
    assert np.allclose(out, 2048 / 32768.0, atol=1e-4)


def test_latest_zero_pads_when_underfilled() -> None:
    eng = AudioEngine()
    eng._write(_frames(100, value=3000))
    out = eng.latest(500)
    assert out.shape == (500,)
    assert np.all(out[:400] == 0.0)  # left-padded
    assert np.allclose(out[400:], 3000 / 32768.0, atol=1e-4)


def test_drop_oldest_on_overflow() -> None:
    eng = AudioEngine()
    cap = eng._capacity
    eng._write(_frames(cap, value=100))
    eng._write(_frames(1000, value=777))  # forces drop-oldest
    out = eng.latest(1000)
    assert np.allclose(out, 777 / 32768.0, atol=1e-4)
    # buffered never exceeds capacity
    assert eng._buffered == cap


def test_write_larger_than_capacity() -> None:
    eng = AudioEngine()
    cap = eng._capacity
    eng._write(_frames(cap + 5000, value=42))
    assert eng._buffered == cap
    out = eng.latest(10)
    assert np.allclose(out, 42 / 32768.0, atol=1e-4)


def test_read_pops_and_pads() -> None:
    eng = AudioEngine()
    eng._write(_frames(50, value=1234))
    scratch = np.empty((100, 2), dtype=np.int16)
    got = eng._read(100, scratch)
    assert got == 50
    assert np.all(scratch[:50] == 1234)


def test_attach_drains_stream_to_eof() -> None:
    eng = AudioEngine()
    # 4096 stereo int16 frames as raw bytes.
    frames = _frames(4096, value=555)
    stream = io.BytesIO(frames.tobytes())
    eng.attach(stream)
    reader = eng._reader
    assert reader is not None
    reader.join(timeout=2.0)
    assert not reader.is_alive()  # exited cleanly on EOF
    assert eng._buffered == 4096
    out = eng.latest(10)
    assert np.allclose(out, 555 / 32768.0, atol=1e-4)


def test_attach_can_be_called_again() -> None:
    eng = AudioEngine()
    eng.attach(io.BytesIO(_frames(1024).tobytes()))
    if eng._reader is not None:
        eng._reader.join(timeout=2.0)
    eng.attach(io.BytesIO(_frames(2048, value=999).tobytes()))
    reader = eng._reader
    assert reader is not None
    reader.join(timeout=2.0)
    assert not reader.is_alive()
    out = eng.latest(10)
    assert np.allclose(out, 999 / 32768.0, atol=1e-4)
