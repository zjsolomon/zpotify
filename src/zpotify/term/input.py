"""Escape-sequence decoder: raw stdin bytes -> Key / Mouse / Resize / Paste.

``InputReader`` wraps fd 0. The UI loop selects on :meth:`fileno`, calls
:meth:`read` when readable, and dispatches the returned events. Incomplete
escape sequences (including a lone ESC, which is ambiguous until the next byte)
stay buffered: when :attr:`pending_escape` is set the caller waits ~25 ms and,
if no more bytes arrive, calls :meth:`flush_escape` to emit the lone ESC.

Resize: call :meth:`install_resize_handler` to trap SIGWINCH. The handler only
sets a flag; the next :meth:`read` prepends a :class:`Resize` event, so resize
is delivered on the same thread as every other event.
"""

from __future__ import annotations

import os
import signal
from dataclasses import replace

from .events import Event, Key, Mouse, Paste, Resize

# CSI final byte (letter) -> special key name.
_CSI_LETTER = {
    "A": "up", "B": "down", "C": "right", "D": "left",
    "H": "home", "F": "end", "Z": "backtab",
    "P": "f1", "Q": "f2", "R": "f3", "S": "f4",
}
# SS3 final byte -> key name (F1-F4).
_SS3 = {"P": "f1", "Q": "f2", "R": "f3", "S": "f4"}
# CSI "<n>~" numeric parameter -> key name.
_CSI_TILDE = {
    1: "home", 7: "home", 4: "end", 8: "end",
    2: "insert", 3: "delete", 5: "pgup", 6: "pgdn",
    15: "f5", 17: "f6", 18: "f7", 19: "f8", 20: "f9",
    21: "f10", 23: "f11", 24: "f12",
}

_PASTE_END = b"\x1b[201~"


class InputReader:
    """Incremental terminal input parser."""

    def __init__(self, fd: int = 0) -> None:
        self.fd = fd
        self._buf = bytearray()
        self.pending_escape = False
        self._resized = False
        self._resize_cb = None

    def fileno(self) -> int:
        """Underlying file descriptor, for use with ``selectors``."""
        return self.fd

    # -- resize ----------------------------------------------------------

    def install_resize_handler(self, callback=None) -> None:
        """Trap SIGWINCH. ``callback`` (if given) runs in the signal handler;
        a :class:`Resize` event is also emitted from the next :meth:`read`."""
        self._resize_cb = callback

        def _handler(signum: int, frame: object) -> None:
            self._resized = True
            if self._resize_cb is not None:
                self._resize_cb()

        signal.signal(signal.SIGWINCH, _handler)

    # -- reading ---------------------------------------------------------

    def read(self) -> list[Event]:
        """Read available bytes and return every complete event parsed."""
        try:
            data = os.read(self.fd, 4096)
        except (BlockingIOError, InterruptedError):
            data = b""
        if data:
            self._buf.extend(data)
        events: list[Event] = []
        if self._resized:
            self._resized = False
            cols, rows = os.get_terminal_size()
            events.append(Resize(cols, rows))
        events.extend(self._parse())
        return events

    def feed(self, data: bytes) -> list[Event]:
        """Test hook: inject raw bytes as if read from the tty."""
        self._buf.extend(data)
        return self._parse()

    def flush_escape(self) -> list[Event]:
        """Force a buffered lone ESC to be emitted as ``Key(name='esc')``."""
        if self._buf and self._buf[0] == 0x1B:
            del self._buf[0]
            self.pending_escape = False
            return [Key(name="esc")] + self._parse()
        return []

    # -- parser core -----------------------------------------------------

    def _parse(self) -> list[Event]:
        events: list[Event] = []
        self.pending_escape = False
        while self._buf:
            if self._buf[0] == 0x1B:
                result = self._parse_escape(self._buf)
                if result is None:
                    self.pending_escape = True
                    break
            else:
                result = self._parse_key(self._buf)
                if result is None:
                    break  # incomplete multibyte UTF-8
            n, evs = result
            del self._buf[:n]
            events.extend(evs)
        return events

    def _parse_key(self, buf: bytearray) -> tuple[int, list[Event]] | None:
        """Parse one non-escape key. ``None`` -> incomplete multibyte char."""
        b = buf[0]
        if b in (0x0D, 0x0A):
            return 1, [Key(name="enter")]
        if b == 0x09:
            return 1, [Key(name="tab")]
        if b in (0x7F, 0x08):
            return 1, [Key(name="backspace")]
        if b == 0x20:
            return 1, [Key(name="space", char=" ")]
        if 1 <= b <= 26:  # ctrl+letter (tab/enter already handled above)
            return 1, [Key(char=chr(b - 1 + ord("a")), ctrl=True)]
        if b < 0x80:
            return 1, [Key(char=chr(b))]
        # Multibyte UTF-8 lead byte.
        if b >= 0xF0:
            need = 4
        elif b >= 0xE0:
            need = 3
        else:
            need = 2
        if len(buf) < need:
            return None
        try:
            ch = bytes(buf[:need]).decode("utf-8")
        except UnicodeDecodeError:
            return 1, []  # drop the bad byte, keep going
        return need, [Key(char=ch)]

    def _parse_escape(self, buf: bytearray) -> tuple[int, list[Event]] | None:
        """Parse an ESC-prefixed sequence. ``None`` -> incomplete."""
        if len(buf) == 1:
            return None  # lone ESC so far
        b1 = buf[1]
        if b1 == ord("["):
            return self._parse_csi(buf)
        if b1 == ord("O"):
            return self._parse_ss3(buf)
        # ESC + <key> == Alt+key (recurses so Alt+arrow etc. also works).
        inner = self._parse_escape(buf[1:]) if b1 == 0x1B \
            else self._parse_key(buf[1:])
        if inner is None:
            return None
        n, evs = inner
        return 1 + n, [self._with_alt(e) for e in evs]

    def _parse_ss3(self, buf: bytearray) -> tuple[int, list[Event]] | None:
        if len(buf) < 3:
            return None
        name = _SS3.get(chr(buf[2]))
        if name is None:
            return 3, []
        return 3, [Key(name=name)]

    def _parse_csi(self, buf: bytearray) -> tuple[int, list[Event]] | None:
        # Scan for the final byte (0x40..0x7e).
        i = 2
        while i < len(buf) and not (0x40 <= buf[i] <= 0x7E):
            i += 1
        if i >= len(buf):
            return None  # incomplete
        final = chr(buf[i])
        body = bytes(buf[2:i])
        seq_len = i + 1

        # Bracketed paste: buffer the whole span up to the end marker.
        if body == b"200" and final == "~":
            end = buf.find(_PASTE_END, seq_len)
            if end == -1:
                return None
            text = bytes(buf[seq_len:end]).decode("utf-8", "replace")
            return end + len(_PASTE_END), [Paste(text)]

        if body[:1] == b"<":
            return seq_len, self._mouse(body[1:], final)

        params = [int(p) if p else 0 for p in body.split(b";")] if body else []

        if final in _CSI_LETTER:
            ctrl, alt, shift = self._mods(params, 1)
            return seq_len, [Key(name=_CSI_LETTER[final], ctrl=ctrl,
                                 alt=alt, shift=shift)]
        if final == "~":
            num = params[0] if params else 0
            name = _CSI_TILDE.get(num)
            if name is None:
                return seq_len, []
            ctrl, alt, shift = self._mods(params, 1)
            return seq_len, [Key(name=name, ctrl=ctrl, alt=alt, shift=shift)]
        return seq_len, []

    def _mouse(self, body: bytes, final: str) -> list[Event]:
        try:
            b, sx, sy = (int(p) for p in body.split(b";"))
        except ValueError:
            return []
        shift = bool(b & 4)
        alt = bool(b & 8)
        ctrl = bool(b & 16)
        low = b & 3
        x, y = sx - 1, sy - 1
        if b & 64:  # scroll wheel
            kind = "scroll_up" if low == 0 else "scroll_down"
            button = 0
        elif final == "m":  # SGR release
            kind, button = "release", low + 1
        elif b & 32:  # motion
            if low == 3:
                kind, button = "move", 0
            else:
                kind, button = "drag", low + 1
        else:  # press
            kind, button = "press", low + 1
        return [Mouse(x=x, y=y, kind=kind, button=button,
                      ctrl=ctrl, alt=alt, shift=shift)]

    @staticmethod
    def _mods(params: list[int], idx: int) -> tuple[bool, bool, bool]:
        """Decode a CSI modifier param -> (ctrl, alt, shift)."""
        if len(params) <= idx or params[idx] == 0:
            return False, False, False
        bits = params[idx] - 1
        return bool(bits & 4), bool(bits & 2), bool(bits & 1)

    @staticmethod
    def _with_alt(event: Event) -> Event:
        return replace(event, alt=True) if isinstance(event, Key) else event
