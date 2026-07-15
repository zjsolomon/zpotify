"""Devices: Spotify Connect devices; enter transfers playback."""

from __future__ import annotations

from zpotify.models import Device
from zpotify.term.events import Key
from zpotify.term.screen import Screen
from zpotify.term.style import Style
from zpotify.term.widgets import ListView
from zpotify.ui import theme
from zpotify.ui.views import common
from zpotify.ui.views.base import View


def _device_row(device: Device, selected: bool, width: int) -> list[tuple[str, Style]]:
    base = theme.ROW_SELECTED if selected else theme.ROW
    dim = theme.ROW_DIM_SELECTED if selected else theme.ROW_DIM
    mark = "● " if device.is_active else "  "
    name_w = max(10, width - 20)
    name = device.name[:name_w]
    segments = [(" " + mark, theme.ACCENT if device.is_active else dim),
                (name + " " * (name_w - len(name)), base),
                (f" {device.type.lower()}", dim)]
    return segments


class DevicesView(View):
    name = "devices"

    def __init__(self) -> None:
        self.devices = ListView(rows=[], render_row=_device_row)
        self.loading = False

    def on_show(self, app) -> None:
        self.reload(app)

    def reload(self, app) -> None:
        self.loading = True
        def done(rows):
            self.loading = False
            self.devices.rows = rows
        app.call_api(app.api.devices, then=done, refresh=False, describe="devices")

    def handle_key(self, app, key: Key) -> bool:
        if common.list_nav(self.devices, key):
            return True
        if key.char == "R":
            self.reload(app)
            return True
        if key.name == "enter" and self.devices.rows:
            self._transfer(app, self.devices.selected)
            return True
        return False

    def _transfer(self, app, index: int) -> None:
        device = self.devices.rows[index]
        if device.id:
            app.call_api(lambda: app.api.transfer(device.id, play=True),
                         then=lambda _: self.reload(app), describe="transfer")
            app.notify(f"playing on: {device.name}")

    def render(self, app, screen: Screen, x: int, y: int, w: int, h: int) -> None:
        header = "spotify connect devices" + \
            ("  (loading…)" if self.loading else "  (enter transfers, R refreshes)")
        screen.put(x + 2, y, header, theme.DIM)
        if self.devices.rows:
            self.devices.render(screen, x + 1, y + 1, w - 2, h - 1)
            common.wire_list_mouse(app, self.devices, x + 1, y + 1, w - 2, h - 1,
                                   lambda i: self._transfer(app, i))
