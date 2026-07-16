"""PCM ring buffer, sounddevice output, and local volume.

A daemon reader thread drains librespot's stdout (S16LE, 44100 Hz, stereo,
interleaved) into a ring buffer addressed by *absolute* frame positions
(`_written` / `_readpos`). The write side BLOCKS at a small fill limit
(~0.3 s): that backpressure propagates through the OS pipe to librespot, so
the whole chain is paced by the audio device consuming frames in real time,
and controls stay responsive because little audio sits downstream.

Pause is local and instant: :meth:`hold` gates consumption (after an optional
quick fade) without losing buffer state; :meth:`release` resumes it.

A sounddevice output callback pulls blocks, applies local volume and the fade
envelope (pause fades / self-heal); on underrun it pads silence and re-arms a
small prebuffer gate. The visualizer tap records what was actually consumed,
pre-volume.
"""

from __future__ import annotations

import threading
from typing import BinaryIO

import numpy as np

# Estimated seconds of PCM sitting in the OS pipe between librespot and us.
_PIPE_SECONDS = 0.37


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
        capacity_seconds: float = 2.0,
    ) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize
        self.volume = volume

        self._capacity = max(blocksize * 8, int(samplerate * capacity_seconds))
        self._fill_limit = max(blocksize * 4, int(samplerate * buffer_seconds))
        self._prime_frames = min(self._fill_limit // 2,
                                 int(samplerate * prebuffer_seconds))
        self._ring = np.zeros((self._capacity, channels), dtype=np.int16)
        self._written = 0   # absolute frames received from librespot
        self._readpos = 0   # absolute frames consumed by the callback
        # One condition guards everything; writers wait on it at the fill
        # limit and the callback notifies as it frees space.
        self._cond = threading.Condition()
        self._primed = False

        # Fade envelope: a 0..1 gain ramp stepped per callback block (~23 ms),
        # multiplied into the output after volume. Drives pause fades and the
        # app's self-heal.
        self._env = 1.0
        self._env_target = 1.0
        self._env_rate = 0.0  # per-frame delta

        # Local pause: hold gates consumption entirely (buffer preserved).
        self._held = False
        self._hold_pending = False

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

    @property
    def _buffered(self) -> int:
        return self._written - self._readpos

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

    def _copy_in(self, abs_pos: int, frames: np.ndarray) -> None:
        idx = abs_pos % self._capacity
        first = min(len(frames), self._capacity - idx)
        self._ring[idx:idx + first] = frames[:first]
        rest = len(frames) - first
        if rest:
            self._ring[:rest] = frames[first:]

    def _copy_out(self, abs_pos: int, n: int) -> np.ndarray:
        idx = abs_pos % self._capacity
        first = min(n, self._capacity - idx)
        if first == n:
            return self._ring[idx:idx + n]
        out = np.empty((n, self.channels), dtype=np.int16)
        out[:first] = self._ring[idx:idx + first]
        out[first:] = self._ring[:n - first]
        return out

    def _write(self, frames: np.ndarray) -> None:
        """Push ``(n, channels)`` int16 frames, BLOCKING at the fill limit.

        This is the backpressure that paces librespot: when we won't accept
        more we wait for the audio callback to consume, which in turn lets
        the OS pipe fill and librespot's own write block.
        """
        n = len(frames)
        offset = 0
        while offset < n and not self._reader_stop.is_set():
            with self._cond:
                free = self._fill_limit - self._buffered
                if free <= 0:
                    self._cond.wait(timeout=0.1)
                    continue
                take = min(free, n - offset)
                self._copy_in(self._written, frames[offset:offset + take])
                self._written += take
                offset += take

    def _read(self, n: int, out: np.ndarray) -> int:
        """Pop up to ``n`` frames into ``out``; return how many were available.

        Also feeds the visualizer tap and the smoothed RMS level, so both
        always describe the audio actually being consumed (pre-volume).
        """
        with self._cond:
            m = min(n, self._buffered)
            if m > 0:
                out[:m] = self._copy_out(self._readpos, m)
                self._readpos += m
                self._cond.notify()
                mono = out[:m].astype(np.float32).mean(axis=1) / 32768.0
                self._tap_write(mono)
                rms = float(np.sqrt(np.mean(mono ** 2)))
            else:
                rms = 0.0
            self._level += (rms - self._level) * 0.3
        return m

    # -- envelope / pause ---------------------------------------------------

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

    def set_env(self, value: float) -> None:
        """Set the envelope instantly."""
        with self._cond:
            self._env = min(1.0, max(0.0, value))
            self._env_target = self._env
            self._env_rate = 0.0

    def hold(self, fade_seconds: float = 0.0) -> None:
        """Gate consumption locally (instant pause). Buffer state is kept, so
        :meth:`release` resumes exactly where the listener left off."""
        if fade_seconds > 0.001:
            self.fade_to(0.0, fade_seconds)
            with self._cond:
                self._hold_pending = True
        else:
            with self._cond:
                self._env = 0.0
                self._env_target = 0.0
                self._env_rate = 0.0
                self._held = True

    def release(self, fade_seconds: float = 0.15) -> None:
        with self._cond:
            was_held = self._held or self._hold_pending
            self._held = False
            self._hold_pending = False
        if was_held or self._env_target < 1.0:
            self.fade_to(1.0, fade_seconds)

    @property
    def held(self) -> bool:
        return self._held or self._hold_pending

    @property
    def env(self) -> float:
        return self._env

    @property
    def env_target(self) -> float:
        return self._env_target

    @property
    def latency(self) -> float:
        """Estimated seconds between librespot's write position and the
        speakers: buffered frames plus the OS pipe."""
        with self._cond:
            buffered = self._buffered
        return buffered / self.samplerate + _PIPE_SECONDS

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
        """Drop everything buffered (skip/seek) and re-arm the prebuffer."""
        with self._cond:
            self._readpos = self._written
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
            if self._held:
                outdata[:] = 0
                self._level += (0.0 - self._level) * 0.3
                return
            # Prebuffer gate: after a flush/underrun, wait for a little audio
            # to accumulate before playing so track starts don't stutter.
            with self._cond:
                if not self._primed:
                    if self._buffered >= self._prime_frames:
                        self._primed = True
                    else:
                        outdata[:] = 0
                        self._level += (0.0 - self._level) * 0.3  # decay while gated
                        return
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
            if self._hold_pending and self._env <= 0.001:
                with self._cond:
                    self._held = True
                    self._hold_pending = False
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
