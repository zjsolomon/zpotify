"""Input event types produced by term.input and consumed by the UI loop."""

from __future__ import annotations

from dataclasses import dataclass

# Key.name values for non-printable keys. Printable keys have name == "" and
# char set to the character. Space is name "space" (char " ").
KEY_NAMES = frozenset({
    "enter", "esc", "tab", "backtab", "backspace", "delete", "space",
    "up", "down", "left", "right", "home", "end", "pgup", "pgdn", "insert",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
})


@dataclass(frozen=True)
class Key:
    char: str = ""          # printable character, if any
    name: str = ""          # one of KEY_NAMES for special keys
    ctrl: bool = False
    alt: bool = False
    shift: bool = False     # only reported for special keys / mouse

    def __str__(self) -> str:
        mods = "".join(m for m, on in (("C-", self.ctrl), ("M-", self.alt),
                                       ("S-", self.shift)) if on)
        return mods + (self.name or self.char)


@dataclass(frozen=True)
class Mouse:
    x: int                  # 0-based column
    y: int                  # 0-based row
    kind: str               # press | release | drag | move | scroll_up | scroll_down
    button: int = 0         # 1=left 2=middle 3=right, 0 if n/a
    ctrl: bool = False
    alt: bool = False
    shift: bool = False


@dataclass(frozen=True)
class Resize:
    cols: int
    rows: int


@dataclass(frozen=True)
class Paste:
    text: str


Event = Key | Mouse | Resize | Paste
