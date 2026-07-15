"""Now Playing: big track info plus the audio-driven visualizer."""

from __future__ import annotations

from zpotify.player.fft import waveform
from zpotify.term.screen import Screen
from zpotify.term.style import Style
from zpotify.ui import theme
from zpotify.ui.views.base import View

EIGHTHS = " ▁▂▃▄▅▆▇█"


class NowPlayingView(View):
    name = "now playing"

    def render(self, app, screen: Screen, x: int, y: int, w: int, h: int) -> None:
        state = app.playback
        track = state.track if state else None
        info_h = 4
        cy = y + 1
        if track is not None:
            screen.put(x + 3, cy, track.name[:w - 6],
                       Style(fg=theme.WHITE, bg=theme.BG, bold=True))
            screen.put(x + 3, cy + 1, track.artist[:w - 6], theme.ACCENT)
            album = track.album + ("  [E]" if track.explicit else "")
            screen.put(x + 3, cy + 2, album[:w - 6], theme.DIM)
        else:
            screen.put(x + 3, cy, "nothing playing", theme.DIM)
            screen.put(x + 3, cy + 2,
                       "press / to search, 3 for playlists, ? for help", theme.FAINT)

        viz_y = y + info_h + 1
        viz_h = h - info_h - 2
        if viz_h < 2 or app.visualizer == "off":
            return
        if app.visualizer == "spectrum":
            self._render_spectrum(app, screen, x + 2, viz_y, w - 4, viz_h)
        else:
            self._render_wave(app, screen, x + 2, viz_y, w - 4, viz_h)

    # bars: one column of width 2 per bin, height in cell-eighths
    def _render_spectrum(self, app, screen: Screen, x: int, y: int, w: int, h: int) -> None:
        bars = app.analyzer.bars
        peaks = app.analyzer.peaks
        n = len(bars)
        col_w = max(1, w // n)
        for i in range(min(n, w // col_w)):
            frac = float(bars[i])
            color = theme.spectrum_color(frac)
            style = Style(fg=color, bg=theme.BG)
            total_eighths = int(frac * h * 8)
            full, rem = divmod(total_eighths, 8)
            cx = x + i * col_w
            for row in range(full):
                screen.put(cx, y + h - 1 - row, EIGHTHS[8] * (col_w - (col_w > 1)), style)
            if rem and full < h:
                screen.put(cx, y + h - 1 - full,
                           EIGHTHS[rem] * (col_w - (col_w > 1)), style)
            peak_row = int(float(peaks[i]) * h * 8) // 8
            if 0 < peak_row < h and peak_row > full:
                screen.put(cx, y + h - 1 - peak_row,
                           "─" * (col_w - (col_w > 1)), theme.FAINT)

    def _render_wave(self, app, screen: Screen, x: int, y: int, w: int, h: int) -> None:
        samples = app.audio.latest(4096)
        amps = waveform(samples, w)
        mid = y + h // 2
        half = max(1, h // 2)
        for i in range(w):
            amp = float(amps[i]) if i < len(amps) else 0.0
            span = max(0, min(half, round(amp * half)))
            color = theme.spectrum_color(amp)
            style = Style(fg=color, bg=theme.BG)
            if span == 0:
                screen.put(x + i, mid, "─", theme.FAINT)
                continue
            for dy in range(span):
                char = "█" if dy < span - 1 else "▊"
                screen.put(x + i, mid - dy, char, style)
                screen.put(x + i, min(mid + dy, y + h - 1), char, style)

    def handle_key(self, app, key) -> bool:
        return False
