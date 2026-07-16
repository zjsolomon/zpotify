"""Settings: edit config in-app; changes persist immediately.

Player-engine settings (quality, normalization) restart librespot; the app
debounces that so cycling through values causes one restart, not three.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from zpotify.term.events import Key
from zpotify.term.screen import Screen
from zpotify.term.style import Style
from zpotify.term.widgets import ListView
from zpotify.ui import theme
from zpotify.ui.views import common
from zpotify.ui.views.base import View


@dataclass
class Setting:
    label: str
    description: str
    options: list[tuple[Any, str]]        # (value, display)
    get: Callable[[], Any]
    set: Callable[[Any], None]
    needs_restart: bool = False

    def current_display(self) -> str:
        value = self.get()
        for candidate, display in self.options:
            if candidate == value:
                return display
        return str(value)

    def cycle(self, delta: int) -> None:
        values = [v for v, _ in self.options]
        try:
            index = values.index(self.get())
        except ValueError:
            index = 0
        self.set(values[(index + delta) % len(values)])


def _build_settings(app) -> list[Setting]:
    config = app.config

    def set_bitrate(v):
        config.bitrate = v

    def set_fade(v):
        config.fade_seconds = v

    def set_pause_fade(v):
        config.pause_fade = v

    def set_norm(v):
        config.normalization = v

    def set_visualizer(v):
        config.visualizer = v
        app.visualizer = v  # applies live

    onoff = [(True, "on"), (False, "off")]
    return [
        Setting("streaming quality", "audio bitrate from Spotify (restarts the player)",
                [(96, "96 kbps"), (160, "160 kbps"), (320, "320 kbps")],
                lambda: config.bitrate, set_bitrate, needs_restart=True),
        Setting("track fade in/out", "fade in at track start and out approaching its end",
                [(0.0, "off"), (1.0, "1 s"), (2.0, "2 s"), (3.0, "3 s"),
                 (5.0, "5 s"), (8.0, "8 s"), (12.0, "12 s")],
                lambda: config.fade_seconds, set_fade),
        Setting("pause/resume fade", "short fade instead of hard cuts when pausing",
                onoff, lambda: config.pause_fade, set_pause_fade),
        Setting("volume normalization", "play tracks at similar loudness (restarts the player)",
                onoff, lambda: config.normalization, set_norm, needs_restart=True),
        Setting("visualizer", "default mode for the now-playing view",
                [("spectrum", "spectrum"), ("wave", "wave"), ("off", "off")],
                lambda: config.visualizer, set_visualizer),
    ]


def _setting_row(setting: Setting, selected: bool, width: int) -> list[tuple[str, Style]]:
    base = theme.ROW_SELECTED if selected else theme.ROW
    value_style = theme.ACCENT_BOLD if selected else theme.ACCENT
    value = f"◀ {setting.current_display():^10} ▶" if selected \
        else f"  {setting.current_display():^10}  "
    label_w = max(10, width - len(value) - 3)
    label = setting.label[:label_w]
    return [(" " + label + " " * (label_w - len(label)) + " ", base),
            (value, value_style)]


class SettingsView(View):
    name = "settings"

    def __init__(self) -> None:
        self.listview = ListView(rows=[], render_row=_setting_row)
        self._built = False

    def on_show(self, app) -> None:
        if not self._built:
            self.listview.rows = _build_settings(app)
            self._built = True

    def _change(self, app, delta: int) -> None:
        if not self.listview.rows:
            return
        setting: Setting = self.listview.rows[self.listview.selected]
        setting.cycle(delta)
        app.config.save()
        app.notify(f"{setting.label}: {setting.current_display()}")
        if setting.needs_restart:
            app.schedule_player_restart()

    def handle_key(self, app, key: Key) -> bool:
        if common.list_nav(self.listview, key):
            return True
        if key.name in ("enter", "right", "space"):
            self._change(app, 1)
            return True
        if key.name == "left":
            self._change(app, -1)
            return True
        return False

    def render(self, app, screen: Screen, x: int, y: int, w: int, h: int) -> None:
        self.on_show(app)
        screen.put(x + 2, y, "settings — enter/→ next value, ← previous; saved instantly",
                   theme.DIM)
        list_h = min(len(self.listview.rows), h - 4)
        if list_h > 0:
            self.listview.render(screen, x + 1, y + 2, w - 2, list_h)

            def on_click(mouse):
                if mouse.kind in ("scroll_up", "scroll_down"):
                    self.listview.scroll(-1 if mouse.kind == "scroll_up" else 1)
                elif mouse.kind == "press":
                    index = self.listview.click(mouse.y - (y + 2))
                    if index is not None:
                        self._change(app, 1)
            app.add_hit(x + 1, y + 2, w - 2, list_h, on_click)
        if self.listview.rows:
            setting = self.listview.rows[self.listview.selected]
            screen.put(x + 2, y + 3 + list_h, setting.description[:w - 4], theme.FAINT)
