"""PCM ring buffer, sounddevice output, and local volume.

A daemon reader thread drains librespot's stdout (S16LE, 44100 Hz, stereo,
interleaved) into a ~2 second int16 ring buffer.  A sounddevice output callback
pulls blocks from that ring, applies local volume, and writes them to the
speakers; on underrun it pads with silence rather than blocking or raising.

The same ring feeds cheap :meth:`AudioEngine.latest` snapshots to the FFT /
waveform views, and the callback tracks a smoothed RMS :attr:`level` the UI uses
to tell "audio is actually playing" from "connected but idle".
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
    ) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize
        self.volume = volume

        self._capacity = samplerate * 2  # ~2 seconds of frames
        self._ring = np.zeros((self._capacity, channels), dtype=np.int16)
        self._write_idx = 0
        self._read_idx = 0
        self._buffered = 0
        self._lock = threading.Lock()

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
        reader = self._reader
        if reader is not None and reader.is_alive():
            reader.join(timeout=1.0)
        self._reader = None

    # -- ring buffer -----------------------------------------------------

    def _write(self, frames: np.ndarray) -> None:
        """Push ``(n, channels)`` int16 frames into the ring (drop-oldest)."""
        n = len(frames)
        if n == 0:
            return
        if n >= self._capacity:
            frames = frames[-self._capacity:]
            n = self._capacity
        with self._lock:
            first = min(n, self._capacity - self._write_idx)
            self._ring[self._write_idx:self._write_idx + first] = frames[:first]
            rest = n - first
            if rest:
                self._ring[:rest] = frames[first:]
            self._write_idx = (self._write_idx + n) % self._capacity
            self._buffered += n
            if self._buffered > self._capacity:
                overflow = self._buffered - self._capacity
                self._read_idx = (self._read_idx + overflow) % self._capacity
                self._buffered = self._capacity

    def _read(self, n: int, out: np.ndarray) -> int:
        """Pop up to ``n`` frames into ``out``; return how many were available."""
        with self._lock:
            avail = min(n, self._buffered)
            first = min(avail, self._capacity - self._read_idx)
            out[:first] = self._ring[self._read_idx:self._read_idx + first]
            rest = avail - first
            if rest:
                out[first:avail] = self._ring[:rest]
            self._read_idx = (self._read_idx + avail) % self._capacity
            self._buffered -= avail
        return avail

    def latest(self, n: int) -> np.ndarray:
        """Return the last ``n`` frames as float32 mono in [-1, 1] (zero-padded left)."""
        out = np.zeros(n, dtype=np.float32)
        with self._lock:
            avail = min(n, self._buffered)
            if avail:
                end = self._write_idx
                start = (end - avail) % self._capacity
                if start < end:
                    window = self._ring[start:end]
                else:
                    window = np.concatenate(
                        (self._ring[start:], self._ring[:end]), axis=0
                    )
                mono = window.astype(np.float32).mean(axis=1)
                out[n - avail:] = mono / 32768.0
        return out

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
            scratch = self._scratch if frames == self.blocksize else np.empty(
                (frames, self.channels), dtype=np.int16
            )
            got = self._read(frames, scratch)
            if got < frames:
                scratch[got:] = 0
            block = apply_volume(scratch[:frames], self.volume)
            outdata[:] = block
            # Smoothed RMS over the block, normalized to ~[0, 1].
            if got:
                rms = float(np.sqrt(np.mean(
                    (block[:got].astype(np.float32) / 32768.0) ** 2
                )))
            else:
                rms = 0.0
            self._level += (rms - self._level) * 0.3
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
