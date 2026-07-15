"""Base class for tab views.

A view renders into the body rect each frame and gets the keys the app didn't
consume globally. Clickable areas are registered per-frame via app.add_hit()
inside render(); the app clears them before every frame.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from zpotify.term.events import Key
from zpotify.term.screen import Screen

if TYPE_CHECKING:
    from zpotify.ui.app import App


class View:
    name: str = "?"

    def on_show(self, app: "App") -> None:
        """Called when the view becomes active (lazy data loading goes here)."""

    def render(self, app: "App", screen: Screen, x: int, y: int, w: int, h: int) -> None:
        raise NotImplementedError

    def handle_key(self, app: "App", key: Key) -> bool:
        """Return True if the key was consumed."""
        return False

    @property
    def wants_text(self) -> bool:
        """True while a text input is focused: app skips single-char hotkeys."""
        return False
