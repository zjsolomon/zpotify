"""librespot subprocess lifecycle — a Spotify Connect device feeding raw PCM.

We run ``librespot --backend pipe`` so decoded audio (S16LE, 44100 Hz, stereo,
interleaved) is written to the process's stdout, which :class:`~zpotify.player.audio.AudioEngine`
consumes.  Log lines (including the first-run OAuth authorize URL) go to stderr;
a daemon thread parses the handful of lines we care about into
:class:`LibrespotEvent` objects and keeps a rolling tail for diagnostics.

Volume policy: we launch with ``--volume-ctrl fixed --initial-volume 100`` so
that Spotify Connect volume changes never scale the PCM we receive.  Local
playback volume is applied downstream in :class:`AudioEngine`, which keeps the
FFT visualizer fed with full-scale samples regardless of the listening volume.

Restart policy lives with the caller (the UI): this module only reports that
the process exited (``kind="exit"``); it never restarts itself.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from zpotify.config import DEVICE_NAME, LIBRESPOT_CACHE_DIR

_HOMEBREW_PATHS = ("/opt/homebrew/bin/librespot", "/usr/local/bin/librespot")

_AUTH_URL_RE = re.compile(r"https://accounts\.spotify\.com/authorize\S*")


def find_librespot() -> str | None:
    """Locate the librespot binary on PATH or in common Homebrew prefixes."""
    found = shutil.which("librespot")
    if found:
        return found
    for candidate in _HOMEBREW_PATHS:
        if Path(candidate).is_file():
            return candidate
    return None


@dataclass
class LibrespotEvent:
    """A parsed line of interest from librespot's stderr log or player events.

    ``kind`` is one of ``auth_url``, ``connected``, ``playing``, ``paused``,
    ``stopped``, ``end_of_track``, ``error``, ``exit``.  ``data`` carries
    kind-specific extras (e.g. ``{"url": ...}`` for ``auth_url``,
    ``{"code": rc}`` for ``exit``, ``{"track_id", "latency"}`` for
    ``end_of_track``).
    """

    kind: str
    data: dict = field(default_factory=dict)


# Player events (from the --onevent hook) worth surfacing. ``end_of_track``
# fires only when a track plays to natural completion — never for skips or
# seeks — which is exactly the boundary the crossfade mixer needs; the
# transport kinds double as faster poll nudges than stderr parsing.
_PLAYER_EVENT_KINDS = {
    "end_of_track": "end_of_track",
    "playing": "playing",
    "paused": "paused",
    "stopped": "stopped",
}


def _parse_player_event(line: str) -> LibrespotEvent | None:
    """Parse one ``PLAYER_EVENT TRACK_ID POSITION_MS`` hook line, or None."""
    parts = line.split()
    if not parts:
        return None
    kind = _PLAYER_EVENT_KINDS.get(parts[0])
    if kind is None:
        return None
    data: dict = {"event": parts[0]}
    if len(parts) > 1:
        data["track_id"] = parts[1]
    if len(parts) > 2:
        try:
            data["position_ms"] = int(parts[2])
        except ValueError:
            pass
    return LibrespotEvent(kind, data)


def _parse_stderr_line(line: str) -> LibrespotEvent | None:
    """Map a known librespot 0.8 log line to an event, or ``None`` to ignore.

    Matching is deliberately loose (substring / regex) and tolerant of unknown
    lines so that log-format drift degrades to silence rather than crashes.
    """
    match = _AUTH_URL_RE.search(line)
    if match:
        return LibrespotEvent("auth_url", {"url": match.group(0)})

    low = line.lower()
    if "error" in low or "panicked" in low:
        return LibrespotEvent("error", {"line": line.strip()})
    if "authenticated as" in low or "connecting to" in low:
        return LibrespotEvent("connected", {"line": line.strip()})
    if "loading <" in low or ("play track" in low) or "<play>" in low:
        return LibrespotEvent("playing", {"line": line.strip()})
    if "<pause>" in low or "pausing" in low:
        return LibrespotEvent("paused", {"line": line.strip()})
    if "<stop>" in low or "stopped" in low or "stopping" in low:
        return LibrespotEvent("stopped", {"line": line.strip()})
    return None


class Librespot:
    """Manage a librespot Spotify Connect subprocess with the pipe backend."""

    def __init__(
        self,
        name: str = DEVICE_NAME,
        cache_dir: Path = LIBRESPOT_CACHE_DIR,
        bitrate: int = 320,
        normalization: bool = False,
        on_event: Callable[[LibrespotEvent], None] | None = None,
    ) -> None:
        self.name = name
        self.cache_dir = Path(cache_dir)
        self.bitrate = bitrate
        self.normalization = normalization
        self.on_event = on_event
        self._proc: subprocess.Popen[bytes] | None = None
        self._stopping = False
        self._stderr_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._events_thread: threading.Thread | None = None
        self.stderr_tail: deque[str] = deque(maxlen=200)
        self._hook_path = self.cache_dir / "onevent.sh"
        self._events_path = self.cache_dir / "player-events.log"

    @property
    def credentials_cached(self) -> bool:
        """True if librespot has cached credentials under the cache dir."""
        return (self.cache_dir / "credentials.json").is_file()

    def _build_argv(self, binary: str) -> list[str]:
        argv = [
            binary,
            "--name", self.name,
            "--backend", "pipe",
            "--format", "S16",
            "--bitrate", str(self.bitrate),
            "--cache", str(self.cache_dir),
            "--volume-ctrl", "fixed",
            "--initial-volume", "100",
            "--disable-audio-cache",
            # Keeps the sink stop/start (and its player events) aligned with
            # track edges; librespot still *fetches* the next track ahead, so
            # the PCM stream stays continuous through natural boundaries.
            "--disable-gapless",
            # Player events (end_of_track above all) via a tiny hook script
            # appending lines to a log we tail; fired only at real player
            # transitions, unlike the loosely-parsed stderr log.
            "--onevent", str(self._hook_path),
        ]
        if self.normalization:
            argv.append("--enable-volume-normalisation")
        if not self.credentials_cached:
            argv += ["--enable-oauth", "--oauth-port", "5588"]
        return argv

    def start(self) -> None:
        """Launch librespot; PCM appears on :attr:`stdout`.  No-op if running."""
        if self.running:
            return
        binary = find_librespot()
        if binary is None:
            raise FileNotFoundError("librespot binary not found on PATH")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.cache_dir.chmod(0o700)
        except OSError:
            pass
        self._stopping = False
        self._write_event_hook()
        # Unbuffered pipes: the audio reader needs the raw fd so its byte
        # accounting and pipe-fill queries (FIONREAD) see the true stream
        # state — a Python-side buffer would hide bytes from both.
        self._proc = subprocess.Popen(
            self._build_argv(binary),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="librespot-stderr", daemon=True
        )
        self._stderr_thread.start()
        self._events_thread = threading.Thread(
            target=self._read_events, name="librespot-events", daemon=True
        )
        self._events_thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._watch, name="librespot-watchdog", daemon=True
        )
        self._watchdog_thread.start()

    def _emit(self, event: LibrespotEvent) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001 — callback must not kill our thread
                pass

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        # The pipes are unbuffered (see start()); rebuffer stderr locally so
        # readline doesn't degrade to byte-at-a-time reads.
        stderr = io.BufferedReader(proc.stderr)
        for raw in iter(stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if not line:
                continue
            self.stderr_tail.append(line)
            event = _parse_stderr_line(line)
            if event is not None:
                self._emit(event)

    def _write_event_hook(self) -> None:
        """(Re)create the --onevent hook script and truncate the event log."""
        self._events_path.write_text("")
        self._hook_path.write_text(
            "#!/bin/sh\n"
            f'echo "${{PLAYER_EVENT:-}} ${{TRACK_ID:-}} ${{POSITION_MS:-}}"'
            f' >> "{self._events_path}"\n'
        )
        self._hook_path.chmod(self._hook_path.stat().st_mode
                              | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _read_events(self) -> None:
        """Tail the --onevent log, emitting parsed player events.

        Each emitted event carries ``latency``: seconds between the hook
        appending the line (the log's mtime) and us observing it — the
        crossfade boundary math subtracts the frames delivered meanwhile.
        """
        proc = self._proc
        path = self._events_path
        pos = 0
        while proc is not None and not self._stopping:
            try:
                info = os.stat(path)
                if info.st_size > pos:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(pos)
                        chunk = f.read()
                        pos = f.tell()
                    latency = max(0.0, time.time() - info.st_mtime)
                    for line in chunk.splitlines():
                        event = _parse_player_event(line)
                        if event is not None:
                            event.data["latency"] = latency
                            self._emit(event)
            except OSError:
                pass  # log briefly missing (restart): retry next tick
            if proc.poll() is not None:
                break
            time.sleep(0.03)

    def _watch(self) -> None:
        proc = self._proc
        if proc is None:
            return
        rc = proc.wait()
        if not self._stopping:
            self._emit(LibrespotEvent("exit", {"code": rc}))

    @property
    def stdout(self):
        """The raw PCM byte stream, or ``None`` before :meth:`start`."""
        return self._proc.stdout if self._proc is not None else None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        """Terminate librespot (SIGTERM, then SIGKILL after 3s).  Idempotent."""
        proc = self._proc
        if proc is None:
            return
        self._stopping = True
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
