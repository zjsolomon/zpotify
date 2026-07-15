"""Background work for the UI loop.

API calls must never block the render thread. submit() runs a function on a
worker thread; its result (or exception) is queued and a byte is written to a
self-pipe so the selectors-based main loop wakes up. The loop calls drain()
to run the callbacks on the UI thread.
"""

from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class _Done:
    callback: Callable[[Any, BaseException | None], None] | None
    result: Any
    error: BaseException | None


class WorkerPool:
    def __init__(self, threads: int = 3) -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._done: queue.Queue[_Done] = queue.Queue()
        self._read_fd, self._write_fd = os.pipe()
        os.set_blocking(self._read_fd, False)
        self._threads = [
            threading.Thread(target=self._run, daemon=True, name=f"worker-{i}")
            for i in range(threads)
        ]
        for t in self._threads:
            t.start()

    def fileno(self) -> int:
        return self._read_fd

    def submit(self, fn: Callable[[], Any],
               callback: Callable[[Any, BaseException | None], None] | None = None) -> None:
        self._jobs.put((fn, callback))

    def _run(self) -> None:
        while True:
            fn, callback = self._jobs.get()
            try:
                result, error = fn(), None
            except BaseException as exc:  # surfaced to the UI, never fatal here
                result, error = None, exc
            self._done.put(_Done(callback, result, error))
            try:
                os.write(self._write_fd, b"x")
            except OSError:
                return

    def drain(self) -> None:
        """Run completed callbacks on the calling (UI) thread."""
        try:
            while os.read(self._read_fd, 4096):
                pass
        except BlockingIOError:
            pass
        while True:
            try:
                done = self._done.get_nowait()
            except queue.Empty:
                return
            if done.callback is not None:
                done.callback(done.result, done.error)
