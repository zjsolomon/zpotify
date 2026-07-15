"""Cell styling for the terminal renderer. Truecolor only (24-bit)."""

from __future__ import annotations

from dataclasses import dataclass, replace

RGB = tuple[int, int, int]


@dataclass(frozen=True)
class Style:
    fg: RGB | None = None
    bg: RGB | None = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False

    def with_(self, **kw) -> "Style":
        return replace(self, **kw)

    def sgr(self) -> str:
        """ANSI SGR sequence that switches the terminal to this style (from reset)."""
        parts = ["0"]
        if self.bold:
            parts.append("1")
        if self.dim:
            parts.append("2")
        if self.italic:
            parts.append("3")
        if self.underline:
            parts.append("4")
        if self.reverse:
            parts.append("7")
        if self.fg is not None:
            parts.append(f"38;2;{self.fg[0]};{self.fg[1]};{self.fg[2]}")
        if self.bg is not None:
            parts.append(f"48;2;{self.bg[0]};{self.bg[1]};{self.bg[2]}")
        return "\x1b[" + ";".join(parts) + "m"


DEFAULT = Style()
