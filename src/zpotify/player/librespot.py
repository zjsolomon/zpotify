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

import re
import shutil
import subprocess
import threading
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
    """A parsed line of interest from librespot's stderr log.

    ``kind`` is one of ``auth_url``, ``connected``, ``playing``, ``paused``,
    ``stopped``, ``error``, ``exit``.  ``data`` carries kind-specific extras
    (e.g. ``{"url": ...}`` for ``auth_url``, ``{"code": rc}`` for ``exit``).
    """

    kind: str
    data: dict = field(default_factory=dict)


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
        self.stderr_tail: deque[str] = deque(maxlen=200)

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
        self._proc = subprocess.Popen(
            self._build_argv(binary),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1 << 20,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="librespot-stderr", daemon=True
        )
        self._stderr_thread.start()
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
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if not line:
                continue
            self.stderr_tail.append(line)
            event = _parse_stderr_line(line)
            if event is not None:
                self._emit(event)

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
