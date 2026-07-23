"""PCM ring buffer, sounddevice output, crossfade mixing, and local volume.

A daemon reader thread drains librespot's stdout (S16LE, 44100 Hz, stereo,
interleaved) into a large ring buffer addressed by *absolute* frame positions
(`_written` / `_readpos`). The write side BLOCKS at a soft fill limit: that
backpressure propagates through the OS pipe to librespot, so the whole chain
is paced by the audio device consuming frames in real time.

The fill limit is small (~0.3 s) for responsive controls — unless crossfade
is enabled, in which case it grows to hold the overlap: crossfading needs the
tail of track N *and* the head of track N+1 in memory simultaneously. Track
boundaries come from librespot's ``end_of_track`` player event (fired only on
natural completion, never on skips or seeks): :meth:`note_end_of_track`
converts it to an exact stream position — frames in the ring, plus frames
stuck in the reader's blocked write, plus frames still in the OS pipe
(FIONREAD). When playback reaches ``boundary - X`` the consumer mixes
``ring[cursor]`` (outgoing tail, equal-power fade out) with
``ring[cursor + X]`` (incoming head, equal-power fade in) for X frames, then
jumps the cursor past the head region it already played.

Pause is local and instant: :meth:`hold` gates consumption (after an optional
quick fade) without losing buffer state; :meth:`release` resumes it.

A sounddevice output callback pulls blocks, applies local volume and the fade
envelope (pause fades / self-heal); on underrun it pads silence and re-arms a
small prebuffer gate. The visualizer tap records what was actually consumed —
including the crossfaded mix — pre-volume.
"""

from __future__ import annotations

import fcntl
import select
import struct
import termios
import threading
from collections import deque
from typing import BinaryIO

import numpy as np

# Estimated seconds of PCM sitting in the OS pipe between librespot and us.
_PIPE_SECONDS = 0.37

# Reader wake-up interval while waiting for pipe data (also bounds how long
# a stop request can go unnoticed).
_SELECT_TIMEOUT = 0.05


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
    """Ring-buffered PCM playback with crossfade and a spectrum/waveform tap."""

    def __init__(
        self,
        samplerate: int = 44100,
        channels: int = 2,
        blocksize: int = 1024,
        volume: float = 0.8,
        buffer_seconds: float = 0.3,
        prebuffer_seconds: float = 0.1,
        capacity_seconds: float = 28.0,
    ) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize
        self.volume = volume

        # Physical capacity is generous (a few MB), allocated once and never
        # resized; the *fill limit* is what actually bounds how far librespot
        # runs ahead of the speakers.
        self._capacity = max(blocksize * 8, int(samplerate * capacity_seconds))
        self._base_fill = max(blocksize * 4, int(samplerate * buffer_seconds))
        self._fill_limit = self._base_fill
        self._prime_frames = min(self._base_fill // 2,
                                 int(samplerate * prebuffer_seconds))
        self._ring = np.zeros((self._capacity, channels), dtype=np.int16)
        self._written = 0   # absolute frames received from librespot
        self._readpos = 0   # absolute frames consumed by the callback
        # One condition guards everything; writers wait on it at the fill
        # limit and the callback notifies as it frees space.
        self._cond = threading.Condition()
        self._primed = False

        # Crossfade state.
        self._xfade_frames = 0
        self._boundaries: deque[int] = deque()   # absolute track-end positions
        self._mix_offset = 0   # head tap offset == total mix length (frames)
        self._mix_left = 0     # frames of mix remaining
        self._last_boundary: int | None = None   # last boundary that started playing
        self._boundary_count = 0  # boundaries that reached the speakers (monotonic)
        self._write_backlog = 0  # frames out of the pipe but not yet in the ring

        # Fade envelope: a 0..1 gain ramp stepped per callback block (~23 ms),
        # multiplied into the output after volume. Drives pause fades and the
        # app's self-heal; crossfades are mixed in the ring instead.
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
        self._reader_fd: int | None = None  # raw pipe fd while attached

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
        try:
            fd: int | None = stream.fileno()
        except (OSError, ValueError, AttributeError):
            fd = None  # in-memory stream (tests): plain blocking reads
        self._reader_fd = fd
        # The pipe is unbuffered, so reads may return partial frames; the
        # remainder is carried into the next read — dropping it would shift
        # every later sample's channel/byte alignment.
        pending = b""
        while not self._reader_stop.is_set():
            if fd is not None:
                try:
                    ready, _, _ = select.select([fd], [], [], _SELECT_TIMEOUT)
                except (OSError, ValueError):
                    break  # fd closed underneath us
                if not ready:
                    continue
            try:
                data = stream.read(chunk_bytes)
            except (ValueError, OSError):
                break  # stream closed underneath us
            if not data:
                break  # EOF: librespot exited / restarted
            data = pending + data
            usable = len(data) - (len(data) % frame_bytes)
            pending = data[usable:]
            if usable <= 0:
                continue
            frames = np.frombuffer(data, dtype=np.int16, count=usable // 2)
            self._write(frames.reshape(-1, self.channels))
        self._reader_fd = None

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
                # Frames held here while blocked are neither in the ring nor
                # in the pipe; boundary accounting must still count them.
                self._write_backlog = n - offset
                free = self._fill_limit - self._buffered
                if free <= 0:
                    self._cond.wait(timeout=0.1)
                    continue
                take = min(free, n - offset)
                self._copy_in(self._written, frames[offset:offset + take])
                self._written += take
                offset += take
                self._write_backlog = n - offset
        with self._cond:
            self._write_backlog = 0

    def _advance(self, n: int) -> None:
        """Move the read cursor forward (``self._cond`` must be held)."""
        self._readpos += n
        if n:
            self._cond.notify()

    def _read(self, n: int, out: np.ndarray) -> int:
        """Pop up to ``n`` frames into ``out``, mixing across any track
        boundary that falls inside the crossfade window; return frames
        produced. Also feeds the visualizer tap and the RMS level with what
        was actually played (pre-volume)."""
        with self._cond:
            produced = 0
            while produced < n:
                want = n - produced
                if self._mix_left > 0:
                    x_total = self._mix_offset
                    m = min(want, self._mix_left, self._buffered)
                    if m <= 0:
                        break
                    tail = self._copy_out(self._readpos, m).astype(np.float32)
                    head = np.zeros((m, self.channels), dtype=np.float32)
                    head_avail = self._written - (self._readpos + x_total)
                    hm = max(0, min(m, head_avail))
                    if hm:
                        head[:hm] = self._copy_out(
                            self._readpos + x_total, hm).astype(np.float32)
                    k0 = x_total - self._mix_left
                    theta = (np.arange(k0, k0 + m, dtype=np.float32)
                             / max(1, x_total))[:, None] * (np.pi / 2)
                    # Equal-power curves: constant perceived loudness through
                    # the overlap (cos² + sin² == 1).
                    mixed = tail * np.cos(theta) + head * np.sin(theta)
                    out[produced:produced + m] = np.clip(
                        mixed, -32768, 32767).astype(np.int16)
                    self._advance(m)
                    self._mix_left -= m
                    produced += m
                    if self._mix_left == 0:
                        # skip the head region we already played in the mix
                        self._advance(min(x_total, self._buffered))
                        self._mix_offset = 0
                    continue
                limit = self._buffered
                if self._xfade_frames > 0 and self._boundaries:
                    boundary = self._boundaries[0]
                    dist = boundary - self._readpos
                    if dist <= 0:
                        # boundary reached without enough head to mix: the
                        # next track starts plainly (hard transition)
                        self._boundaries.popleft()
                        self._last_boundary = boundary
                        self._boundary_count += 1
                        continue
                    if dist <= self._xfade_frames:
                        head_avail = self._written - boundary
                        if head_avail >= dist:
                            # enough of the next track is buffered to sustain
                            # the whole overlap: mix `dist` frames, head
                            # tapped `dist` ahead
                            self._boundaries.popleft()
                            self._last_boundary = boundary
                            self._boundary_count += 1
                            self._mix_offset = dist
                            self._mix_left = dist
                            continue
                        # head still buffering: keep playing the tail — the
                        # window shrinks while the head grows, meeting at the
                        # largest overlap the stream can sustain. Chunk only
                        # up to the meet point so it's re-evaluated there.
                        limit = min(limit, max(1, dist - head_avail))
                    else:
                        limit = min(limit, dist - self._xfade_frames)
                m = min(want, limit)
                if m <= 0:
                    break
                out[produced:produced + m] = self._copy_out(self._readpos, m)
                self._advance(m)
                produced += m

            if produced:
                mono = out[:produced].astype(np.float32).mean(axis=1) / 32768.0
                self._tap_write(mono)
                rms = float(np.sqrt(np.mean(mono ** 2)))
            else:
                rms = 0.0
            self._level += (rms - self._level) * 0.3
        return produced

    # -- crossfade / boundaries -------------------------------------------

    def set_crossfade(self, seconds: float) -> None:
        """Set the crossfade overlap; grows/shrinks the fill limit to match.

        The limit is 2×overlap: at mix start the buffer must hold the whole
        outgoing tail (X) *and* enough incoming head (X) to sustain the mix.
        Live-safe: shrinking just lets excess buffered audio drain; growing
        takes effect on the writer's next wakeup.
        """
        seconds = max(0.0, float(seconds))
        with self._cond:
            self._xfade_frames = int(seconds * self.samplerate)
            if self._xfade_frames > 0:
                self._fill_limit = min(
                    self._capacity - self.blocksize * 4,
                    2 * self._xfade_frames + self._base_fill)
            else:
                self._fill_limit = self._base_fill
                self._boundaries.clear()
                self._mix_offset = 0
                self._mix_left = 0
                self._last_boundary = None
            self._cond.notify_all()

    def _pipe_frames(self) -> int:
        """Whole frames currently sitting unread in the OS pipe (0 when the
        attached stream is not a real pipe, e.g. BytesIO in tests)."""
        fd = self._reader_fd
        if fd is None:
            return 0
        try:
            raw = fcntl.ioctl(fd, termios.FIONREAD, struct.pack("i", 0))
            return struct.unpack("i", raw)[0] // (self.channels * 2)
        except (OSError, ValueError):
            return 0

    def note_end_of_track(self, latency_seconds: float = 0.0) -> None:
        """Record 'the current track's last frame has been written'.

        Called (from any thread) when librespot emits its ``end_of_track``
        player event — fired only on natural completion, never for skips or
        seeks. The boundary position counts everything the outgoing track has
        produced: frames in the ring, frames held by a blocked ``_write``,
        and frames still in the OS pipe. ``latency_seconds`` is how stale the
        event observation is; librespot keeps delivering the *next* track's
        frames during that window, so they are subtracted back out.
        """
        with self._cond:
            if self._xfade_frames == 0 or self._buffered == 0:
                return
            lag_frames = int(min(max(0.0, latency_seconds), 0.25) * self.samplerate)
            boundary = (self._written + self._write_backlog
                        + self._pipe_frames() - lag_frames)
            boundary = max(boundary, self._readpos)
            self._boundaries.append(boundary)

    @property
    def transition_pending(self) -> bool:
        """True while a marked track boundary is still ahead of the audible
        position (it clears the moment the crossfade begins). The UI uses
        this to keep showing the outgoing track: with crossfade on, librespot
        reports the track change several seconds before you can hear it."""
        with self._cond:
            return bool(self._boundaries)

    @property
    def boundaries_played(self) -> int:
        """Monotonic count of track boundaries that have reached the speakers
        (mix started or hard transition). Lets the UI detect that a *specific*
        parked track change became audible, immune to earlier boundaries."""
        with self._cond:
            return self._boundary_count

    @property
    def crossed_ms(self) -> int | None:
        """Audible milliseconds into the track that most recently crossed a
        boundary — an exact stream-counted clock for the progress bar during
        and after a crossfade. None when no boundary has played since the
        last flush (seek/skip), where the latency estimate applies instead."""
        with self._cond:
            if self._mix_left > 0:
                played = self._mix_offset - self._mix_left
            elif self._last_boundary is not None:
                played = self._readpos - self._last_boundary
            else:
                return None
            return int(played * 1000 / self.samplerate)

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
            self._boundaries.clear()
            self._mix_offset = 0
            self._mix_left = 0
            self._last_boundary = None
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
