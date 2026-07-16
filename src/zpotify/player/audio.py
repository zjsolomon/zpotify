"""PCM ring buffer, sounddevice output, and local volume.

A daemon reader thread drains librespot's stdout (S16LE, 44100 Hz, stereo,
interleaved) into a small bounded int16 ring buffer.  The write side BLOCKS
when the ring is full: that backpressure propagates through the OS pipe to
librespot, so the whole chain is paced by the audio device consuming frames
in real time.  (Without it, librespot streams an entire track in seconds and
the ring can only keep scraps — audible as the track "playing" instantly as
garbage.)

The ring is deliberately short (~0.3 s) so pause/skip round-trips stay snappy;
together with the OS pipe (~64 KiB ≈ 0.37 s) playback runs roughly 0.7 s behind
librespot's write position.

A sounddevice output callback pulls blocks from the ring, applies local volume,
and writes them to the speakers; on underrun it pads with silence and re-arms a
small prebuffer gate so track starts don't stutter.  The callback also copies
what it just played into a separate visualizer tap, so the FFT/waveform views
see the audio currently audible, not audio still queued.
"""

from __future__ import annotations

import threading
from typing import BinaryIO

import numpy as np


def apply_volume(frames: np.ndarray, volume: float) -> np.ndarray:
    """Scale int16 ``frames`` by ``volume`` (0.0-1.0) using integer math.

    Uses an 8-bit fixed-point multiply ``(x * round(volume*256)) >> 8`` to keep
    the audio callback off the float path.  ``volume <= 1`` so no clipping is
    possible; the result is returned as int16.
    """
    gain = int(round(max(0.0, volume) * 256))
    if gain >= 256:
        return frames
    scaled = (frames.astype(np.int32) * gain) >> 8
    return scaled.astype(np.int16)


class AudioEngine:
    """Ring-buffered PCM playback with a spectrum/waveform tap."""

    def __init__(
        self,
        samplerate: int = 44100,
        channels: int = 2,
        blocksize: int = 1024,
        volume: float = 0.8,
        buffer_seconds: float = 0.3,
        prebuffer_seconds: float = 0.1,
    ) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize
        self.volume = volume

        self._capacity = max(blocksize * 4, int(samplerate * buffer_seconds))
        self._prime_frames = min(self._capacity // 2,
                                 int(samplerate * prebuffer_seconds))
        self._ring = np.zeros((self._capacity, channels), dtype=np.int16)
        self._write_idx = 0
        self._read_idx = 0
        self._buffered = 0
        # One condition guards the ring; writers wait on it when full and the
        # callback notifies as it frees space.
        self._cond = threading.Condition()
        self._primed = False

        # Fade envelope: a 0..1 gain ramp stepped per callback block (~23 ms),
        # multiplied into the output after volume. Drives track fade in/out
        # and click-free pause/resume.
        self._env = 1.0
        self._env_target = 1.0
        self._env_rate = 0.0  # per-frame delta
        # A boundary fade waits for the *next* unprimed->primed transition —
        # the moment a new track's audio actually reaches the speakers, after
        # the natural inter-track underrun (gapless is disabled) — then ramps
        # 0 -> 1. Anchors fade-ins to the audio, not the earlier API events.
        self._boundary_fade_seconds: float | None = None
        self._boundary_deadline = 0.0

        # Visualizer tap: mono float of the most recently *played* frames.
        self._tap_len = 8192
        self._tap = np.zeros(self._tap_len, dtype=np.float32)
        self._tap_idx = 0

        self._stream = None  # sounddevice.OutputStream, opened lazily in start()
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()

        self._level = 0.0
        self.last_error: BaseException | None = None

        # Preallocated scratch for the callback to stay allocation-free.
        self._scratch = np.zeros((blocksize, channels), dtype=np.int16)

    # -- reader thread ---------------------------------------------------

    def attach(self, stream: BinaryIO, chunk_bytes: int = 8192) -> None:
        """Spawn a daemon thread draining ``stream`` (librespot stdout) into the ring.

        Frame count per chunk is ``chunk_bytes // (channels * 2)``.  On EOF (a
        librespot restart) the thread exits cleanly; :meth:`attach` may be
        called again with a fresh stream.
        """
        self._stop_reader()
        self._reader_stop.clear()
        self._reader = threading.Thread(
            target=self._read_loop,
            args=(stream, chunk_bytes),
            name="audio-reader",
            daemon=True,
        )
        self._reader.start()

    def _read_loop(self, stream: BinaryIO, chunk_bytes: int) -> None:
        frame_bytes = self.channels * 2
        # Read whole frames only.
        chunk_bytes -= chunk_bytes % frame_bytes
        while not self._reader_stop.is_set():
            try:
                data = stream.read(chunk_bytes)
            except (ValueError, OSError):
                break  # stream closed underneath us
            if not data:
                break  # EOF: librespot exited / restarted
            usable = len(data) - (len(data) % frame_bytes)
            if usable <= 0:
                continue
            frames = np.frombuffer(data, dtype=np.int16, count=usable // 2)
            self._write(frames.reshape(-1, self.channels))

    def _stop_reader(self) -> None:
        self._reader_stop.set()
        with self._cond:
            self._cond.notify_all()
        reader = self._reader
        if reader is not None and reader.is_alive():
            reader.join(timeout=1.0)
        self._reader = None

    # -- ring buffer -----------------------------------------------------

    def _write(self, frames: np.ndarray) -> None:
        """Push ``(n, channels)`` int16 frames, BLOCKING while the ring is full.

        This is the backpressure that paces librespot: when the ring has no
        space we wait for the audio callback to consume, which in turn lets
        the OS pipe fill and librespot's own write block.
        """
        n = len(frames)
        offset = 0
        while offset < n and not self._reader_stop.is_set():
            with self._cond:
                free = self._capacity - self._buffered
                if free == 0:
                    self._cond.wait(timeout=0.1)
                    continue
                take = min(free, n - offset)
                chunk = frames[offset:offset + take]
                first = min(take, self._capacity - self._write_idx)
                self._ring[self._write_idx:self._write_idx + first] = chunk[:first]
                rest = take - first
                if rest:
                    self._ring[:rest] = chunk[first:]
                self._write_idx = (self._write_idx + take) % self._capacity
                self._buffered += take
                offset += take

    def _read(self, n: int, out: np.ndarray) -> int:
        """Pop up to ``n`` frames into ``out``; return how many were available.

        Also feeds the visualizer tap and the smoothed RMS level, so both
        always describe the audio actually being consumed (pre-volume).
        """
        with self._cond:
            avail = min(n, self._buffered)
            first = min(avail, self._capacity - self._read_idx)
            out[:first] = self._ring[self._read_idx:self._read_idx + first]
            rest = avail - first
            if rest:
                out[first:avail] = self._ring[:rest]
            self._read_idx = (self._read_idx + avail) % self._capacity
            self._buffered -= avail
            if avail:
                mono = out[:avail].astype(np.float32).mean(axis=1) / 32768.0
                self._tap_write(mono)
                rms = float(np.sqrt(np.mean(mono ** 2)))
                self._cond.notify()
            else:
                rms = 0.0
            self._level += (rms - self._level) * 0.3
        return avail

    def fade_to(self, target: float, seconds: float) -> None:
        """Ramp the output envelope to ``target`` (0..1) over ``seconds``."""
        target = min(1.0, max(0.0, target))
        with self._cond:
            self._env_target = target
            if seconds <= 0.001:
                self._env = target
                self._env_rate = 0.0
            else:
                self._env_rate = (target - self._env) / (seconds * self.samplerate)

    def arm_boundary_fade(self, seconds: float, timeout: float = 2.5) -> None:
        """Fade in from silence when the next track's audio starts playing
        (the next unprimed->primed transition in the callback).

        If no underrun occurs within ``timeout`` — librespot loaded the next
        track before the ring drained, so audio flowed continuously — the
        fade fires anyway from the current envelope. Without this fallback a
        fade-out that reached 0 would leave the new track permanently silent.
        """
        import time as _time
        with self._cond:
            self._boundary_fade_seconds = max(0.05, seconds)
            self._boundary_deadline = _time.monotonic() + timeout

    def disarm_boundary_fade(self) -> None:
        with self._cond:
            self._boundary_fade_seconds = None

    @property
    def boundary_fade_armed(self) -> bool:
        return self._boundary_fade_seconds is not None

    @property
    def latency(self) -> float:
        """Estimated seconds between librespot's write position and the
        speakers: our ring buffer content plus the OS pipe (~64 KiB)."""
        with self._cond:
            buffered = self._buffered
        return buffered / self.samplerate + 0.37

    def set_env(self, value: float) -> None:
        """Set the envelope instantly (e.g. to 0 before a track fade-in)."""
        with self._cond:
            self._env = min(1.0, max(0.0, value))
            self._env_target = self._env
            self._env_rate = 0.0

    @property
    def env(self) -> float:
        return self._env

    @property
    def env_target(self) -> float:
        return self._env_target

    def _step_env(self, frames: int) -> float:
        """Advance the envelope by ``frames`` and return the block's mean gain."""
        with self._cond:
            env = self._env
            target = self._env_target
            if env == target or self._env_rate == 0.0:
                return env
            after = env + self._env_rate * frames
            if (self._env_rate > 0 and after >= target) or \
               (self._env_rate < 0 and after <= target):
                after = target
                self._env_rate = 0.0
            self._env = after
            return (env + after) / 2.0

    def flush(self) -> None:
        """Drop everything buffered (e.g. on track change) and re-arm prebuffer."""
        with self._cond:
            self._read_idx = self._write_idx
            self._buffered = 0
            self._primed = False
            self._tap[:] = 0.0
            self._cond.notify_all()

    def latest(self, n: int) -> np.ndarray:
        """Last ``n`` *played* frames as float32 mono in [-1, 1] (zero-padded left)."""
        n = min(n, self._tap_len)
        out = np.empty(n, dtype=np.float32)
        with self._cond:
            end = self._tap_idx
            start = (end - n) % self._tap_len
            if start < end:
                out[:] = self._tap[start:end]
            else:
                split = self._tap_len - start
                out[:split] = self._tap[start:]
                out[split:] = self._tap[:end]
        return out

    def _tap_write(self, mono: np.ndarray) -> None:
        """Record just-played mono float frames for the visualizer (cond held)."""
        n = len(mono)
        if n >= self._tap_len:
            mono = mono[-self._tap_len:]
            n = self._tap_len
        first = min(n, self._tap_len - self._tap_idx)
        self._tap[self._tap_idx:self._tap_idx + first] = mono[:first]
        rest = n - first
        if rest:
            self._tap[:rest] = mono[first:]
        self._tap_idx = (self._tap_idx + n) % self._tap_len

    # -- output stream ---------------------------------------------------

    def start(self) -> None:
        """Open the sounddevice output stream and begin playback."""
        if self._stream is not None:
            return
        import sounddevice as sd  # lazy: keep tests off PortAudio

        self._stream = sd.OutputStream(
            samplerate=self.samplerate,
            channels=self.channels,
            dtype="int16",
            blocksize=self.blocksize,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, outdata: np.ndarray, frames: int, time_info, status) -> None:
        try:
            # Prebuffer gate: after a flush/underrun, wait for a little audio
            # to accumulate before playing so track starts don't stutter.
            with self._cond:
                if not self._primed:
                    if self._buffered >= self._prime_frames:
                        self._primed = True
                        if self._boundary_fade_seconds is not None:
                            # new track's audio is about to play: fade it in
                            seconds = self._boundary_fade_seconds
                            self._boundary_fade_seconds = None
                            self._env = 0.0
                            self._env_target = 1.0
                            self._env_rate = 1.0 / (seconds * self.samplerate)
                    else:
                        outdata[:] = 0
                        self._level += (0.0 - self._level) * 0.3  # decay while gated
                        return
                elif self._boundary_fade_seconds is not None:
                    import time as _time
                    if _time.monotonic() > self._boundary_deadline:
                        # no underrun happened: audio flowed continuously into
                        # the next track — fire the fade from where env sits
                        seconds = self._boundary_fade_seconds
                        self._boundary_fade_seconds = None
                        self._env_target = 1.0
                        self._env_rate = (1.0 - self._env) / (seconds * self.samplerate)
            scratch = self._scratch if frames == self.blocksize else np.empty(
                (frames, self.channels), dtype=np.int16
            )
            got = self._read(frames, scratch)
            if got < frames:
                scratch[got:] = 0
                if got == 0:
                    self._primed = False  # underrun: re-arm the gate
            block = apply_volume(scratch[:frames], self.volume)
            gain = self._step_env(frames)
            if gain < 0.999:
                block = (block.astype(np.float32) * gain).astype(np.int16)
            outdata[:] = block
        except Exception as exc:  # noqa: BLE001 — never let it escape the callback
            self.last_error = exc
            try:
                outdata[:] = 0
            except Exception:  # noqa: BLE001
                pass

    @property
    def level(self) -> float:
        """Recent smoothed RMS in ~[0, 1]; nonzero means audio is flowing."""
        return self._level

    def stop(self) -> None:
        """Stop the reader thread and close the output stream.  Idempotent."""
        self._stop_reader()
        stream = self._stream
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            finally:
                self._stream = None
