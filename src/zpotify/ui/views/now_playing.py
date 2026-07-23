"""Now Playing: big track info plus the audio-driven visualizer."""

from __future__ import annotations

from zpotify.player.fft import waveform
from zpotify.term.screen import Screen
from zpotify.term.style import Style
from zpotify.ui import theme, wordmark
from zpotify.ui.views.base import View

EIGHTHS = " ▁▂▃▄▅▆▇█"


class NowPlayingView(View):
    name = "now playing"

    def __init__(self) -> None:
        self.selected: int | None = None  # highlighted row in UP NEXT

    def handle_key(self, app, key) -> bool:
        count = len(app.up_next[:10])
        if count == 0:
            self.selected = None
            return False
        if key.name == "down" or key.char == "j":
            self.selected = 0 if self.selected is None \
                else min(self.selected + 1, count - 1)
            return True
        if key.name == "up" or key.char == "k":
            if self.selected is not None:
                self.selected = None if self.selected == 0 else self.selected - 1
            return True
        if key.name == "esc" and self.selected is not None:
            self.selected = None
            return True
        if key.name == "enter" and self.selected is not None:
            app.skip_to_queue_index(self.selected)
            self.selected = None
            return True
        return False

    def render(self, app, screen: Screen, x: int, y: int, w: int, h: int) -> None:
        state = app.playback
        track = state.track if state else None
        info_h = 4
        cy = y + 1
        # 8-bit wordmark in the top-right corner (when there's room for it
        # beside the track info)
        text_w = w - 6
        wordmark_shown = w - 6 - wordmark.WIDTH - 4 > 20
        if wordmark_shown:
            wordmark.render(screen, x + w - wordmark.WIDTH - 3, y + 1,
                            body=theme.WHITE, accent=theme.ACCENT_RGB, bg=theme.BG)
            text_w = w - 6 - wordmark.WIDTH - 4
            info_h = max(info_h, wordmark.HEIGHT + 1)  # keep bars off the logo
        if track is not None:
            screen.put(x + 3, cy, track.name[:text_w],
                       Style(fg=theme.WHITE, bg=theme.BG, bold=True))
            screen.put(x + 3, cy + 1, track.artist[:text_w], theme.ACCENT)
            album = track.album + ("  [E]" if track.explicit else "")
            screen.put(x + 3, cy + 2, album[:text_w], theme.DIM)
        else:
            screen.put(x + 3, cy, "nothing playing"[:text_w], theme.DIM)
            # short enough to survive the wordmark clip on narrow terminals
            screen.put(x + 3, cy + 2, "/ search · 3 playlists · ? help"[:text_w],
                       theme.FAINT)

        viz_y = y + info_h + 1
        viz_h = h - info_h - 2
        if viz_h < 2:
            return
        if app.visualizer == "spectrum":
            self._render_spectrum(app, screen, x + 2, viz_y, w - 4, viz_h)
        elif app.visualizer == "wave":
            self._render_wave(app, screen, x + 2, viz_y, w - 4, viz_h)
        self._render_up_next(app, screen, x + 2, viz_y, w - 4, viz_h)

    def _render_up_next(self, app, screen: Screen, x: int, y: int, w: int, h: int) -> None:
        """Boxed queue preview in the bottom-right of the visualizer area.

        Arrow keys highlight a row; enter skips forward to it (Spotify
        semantics: everything before it in the queue is consumed).
        """
        tracks = app.up_next[:10]
        if not tracks or w < 46 or h < 6:
            self.selected = None
            return
        box_w = min(48, w // 2)
        rows = min(len(tracks), h - 3)
        box_h = rows + 2  # border top (with title) + rows + border bottom
        bx = x + w - box_w
        by = y + h - box_h
        if self.selected is not None and self.selected >= rows:
            self.selected = rows - 1
        # solid backdrop so visualizer bars don't bleed through
        screen.fill(bx - 1, by, box_w + 1, box_h, " ", theme.BASE)
        screen.box(bx, by, box_w, box_h,
                   theme.DIM if self.selected is not None else theme.FAINT)
        title = " UP NEXT "
        if getattr(app, "up_next_is_radio", False):
            station = getattr(app, "station", None)
            label = station.label if station is not None else ""
            title = f" UP NEXT · RADIO — {label} " if label else " UP NEXT · RADIO "
            title = title[:box_w - 4]
        screen.put(bx + 2, by, title, theme.DIM.with_(bold=True))
        inner_w = box_w - 4
        for i, t in enumerate(tracks[:rows]):
            selected = i == self.selected
            row_style = theme.ROW_SELECTED if selected else theme.ROW_DIM
            num_style = theme.ROW_SELECTED if selected else theme.FAINT
            number = f"{i + 1:>2} "
            text = (number + f"{t.name} — {t.artist}")[:inner_w].ljust(inner_w)
            screen.put(bx + 2, by + 1 + i, text[:len(number)], num_style)
            screen.put(bx + 2 + len(number), by + 1 + i, text[len(number):], row_style)

            def click(mouse, index=i):
                if mouse.kind == "press":
                    if self.selected == index:
                        app.skip_to_queue_index(index)
                        self.selected = None
                    else:
                        self.selected = index
            app.add_hit(bx + 1, by + 1 + i, box_w - 2, 1, click)
        hint = " ↑↓ + enter plays "
        if self.selected is not None and box_w > len(hint) + 4:
            screen.put(bx + box_w - len(hint) - 2, by + box_h - 1, hint, theme.FAINT)

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
