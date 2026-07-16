"""The application: event loop, layout, global keys, player bar, glue.

Threading model: this loop owns the terminal and all UI state. API calls run
on WorkerPool threads and post callbacks back here. librespot stderr events
are marshaled onto the UI thread through the same pool. The audio callback
thread never touches UI state; we only read AudioEngine.latest()/.level.
"""

from __future__ import annotations

import selectors
import time
from typing import Callable

from zpotify import config as cfg
from zpotify.api import ApiError, SpotifyAPI
from zpotify.auth import Auth, NeedsLogin
from zpotify.models import PlaybackState, Track
from zpotify.player.audio import AudioEngine
from zpotify.player.fft import SpectrumAnalyzer
from zpotify.player.librespot import Librespot, LibrespotEvent
from zpotify.term.events import Key, Mouse, Paste, Resize
from zpotify.term.screen import Screen
from zpotify.term.input import InputReader
from zpotify.term.widgets import ProgressBar, tabs
from zpotify.ui import theme
from zpotify.ui.workers import WorkerPool

FRAME = 1 / 30
POLL_INTERVAL = 2.0
ESC_TIMEOUT = 0.025

HitHandler = Callable[[Mouse], None]


class App:
    def __init__(self, config: cfg.Config, auth: Auth) -> None:
        self.config = config
        self.auth = auth
        self.api = SpotifyAPI(auth)
        self.screen = Screen()
        self.input = InputReader()
        self.workers = WorkerPool()
        # Player commands run on ONE thread so pause/play/seek reach Spotify
        # in the order they were pressed; the shared pool would race them.
        self.control = WorkerPool(threads=1)
        self.analyzer = SpectrumAnalyzer(n_bins=48)
        self.audio = AudioEngine()
        self.audio.volume = config.volume
        self.librespot = self._make_librespot()

        from zpotify.ui.views import (DevicesView, LibraryView, NowPlayingView,
                                      PlaylistsView, QueueView, SearchView,
                                      SettingsView)
        self.views = [NowPlayingView(), SearchView(), PlaylistsView(),
                      LibraryView(), QueueView(), DevicesView(), SettingsView()]
        self.view_index = 0

        self.playback: PlaybackState | None = None
        self._poll_at = 0.0          # monotonic time of last successful poll
        self._next_poll = 0.0
        self.device_id: str | None = None
        self.user_name = ""
        self.visualizer = config.visualizer  # spectrum | wave | off
        self.help_visible = False
        self.quit_confirm = False  # "quit? y" popup is showing
        self.quit_requested = False
        self._status = ""
        self._status_until = 0.0
        self._status_error = False
        self._hits: list[tuple[int, int, int, int, HitHandler]] = []
        self._librespot_auth_url: str | None = None
        self._fade_out_started = False
        self._player_restart_at: float | None = None  # debounced settings restart
        # Optimistic UI: local actions mutate playback state immediately; any
        # poll *requested before* the action is stale and must be discarded,
        # or the icon/progress would flicker back until the next poll.
        self._action_at = 0.0

    def _make_librespot(self) -> Librespot:
        return Librespot(bitrate=self.config.bitrate,
                         normalization=self.config.normalization,
                         on_event=self._on_librespot_event_threaded)

    # ------------------------------------------------------------- lifecycle

    def run(self) -> None:
        with self.screen:
            self.screen.set_title("zpotify")
            self.input.install_resize_handler()
            self._start_playback_stack()
            self.workers.submit(self.api.me, self._on_me)
            sel = selectors.DefaultSelector()
            sel.register(self.input, selectors.EVENT_READ)
            sel.register(self.workers, selectors.EVENT_READ)
            sel.register(self.control, selectors.EVENT_READ)
            next_frame = time.monotonic()
            try:
                while not self.quit_requested:
                    now = time.monotonic()
                    timeout = max(0.0, next_frame - now)
                    if self.input.pending_escape:
                        timeout = min(timeout, ESC_TIMEOUT)
                    for key, _ in sel.select(timeout):
                        if key.fileobj is self.input:
                            for event in self.input.read():
                                self._dispatch(event)
                        elif key.fileobj is self.control:
                            self.control.drain()
                        else:
                            self.workers.drain()
                    if self.input.pending_escape:
                        for event in self.input.flush_escape():
                            self._dispatch(event)
                    now = time.monotonic()
                    if now >= next_frame:
                        next_frame = now + FRAME
                        self._tick(now)
                        self._render()
            finally:
                self._shutdown()

    def _start_playback_stack(self) -> None:
        try:
            self.librespot.start()
        except Exception as exc:
            self.notify(f"librespot failed to start: {exc}", error=True)
            return
        stream = self.librespot.stdout
        if stream is not None:
            self.audio.attach(stream)
        try:
            self.audio.start()
        except Exception as exc:
            self.notify(f"audio output failed: {exc}", error=True)
        self.workers.submit(self._find_our_device, self._on_device_found)

    def _shutdown(self) -> None:
        self.config.volume = round(self.audio.volume, 2)
        self.config.visualizer = self.visualizer
        try:
            self.config.save()
        except OSError:
            pass
        try:
            self.audio.stop()
        finally:
            self.librespot.stop()

    # ------------------------------------------------------------- callbacks

    def _on_me(self, result, error) -> None:
        if error is None and isinstance(result, dict):
            self.user_name = result.get("display_name") or result.get("id", "")
        elif isinstance(error, NeedsLogin):
            self.notify("session expired — run `zpotify auth`", error=True)

    def _find_our_device(self) -> str | None:
        """Worker: wait for our librespot device to appear in Connect."""
        for _ in range(15):
            for device in self.api.devices():
                if device.name == cfg.DEVICE_NAME:
                    return device.id
            time.sleep(1.5)
        return None

    def _on_device_found(self, device_id, error) -> None:
        if error is not None:
            self.notify(f"device discovery failed: {error}", error=True)
            return
        if device_id is None:
            self.notify("librespot device never appeared in Spotify Connect", error=True)
            return
        self.device_id = device_id
        self.notify("connected — press / to search, ? for help")
        self._next_poll = 0.0

    def _on_librespot_event_threaded(self, event: LibrespotEvent) -> None:
        # Called on librespot's stderr thread; marshal to the UI thread.
        self.workers.submit(lambda: event, self._on_librespot_event)

    def _on_librespot_event(self, event: LibrespotEvent, error=None) -> None:
        if event.kind == "auth_url":
            self._librespot_auth_url = event.data.get("url")
        elif event.kind == "exit":
            self.notify("librespot exited — restarting…", error=True)
            self.workers.submit(self._restart_librespot, None)
        elif event.kind in ("playing", "paused", "stopped"):
            if event.kind == "playing" and "loading" in event.data.get("line", "").lower():
                # Natural track change. Do NOT flush here: the previous
                # track's tail is still draining through the ring/pipe and
                # cutting it would clip every ending. (Manual skip/seek flush
                # in their own callbacks.) Just re-arm the fade-in.
                self._begin_fade_in(track_change=True)
            self._next_poll = 0.0  # confirm via API soon

    def _begin_fade_in(self, track_change: bool) -> None:
        """Raise the fade envelope for newly starting audio."""
        self._fade_out_started = False
        if track_change and self.config.fade_seconds > 0:
            self.audio.set_env(0.0)
            self.audio.fade_to(1.0, self.config.fade_seconds)
        else:
            self.audio.fade_to(1.0, 0.15 if self.config.pause_fade else 0.0)

    def _restart_librespot(self) -> None:
        time.sleep(1.0)
        self.librespot.stop()
        self.librespot = self._make_librespot()
        self.librespot.start()
        stream = self.librespot.stdout
        if stream is not None:
            self.audio.attach(stream)
        self.audio.flush()
        self.audio.fade_to(1.0, 0.25)
        self.workers.submit(self._find_our_device, self._on_device_found)

    def schedule_player_restart(self) -> None:
        """Debounced librespot restart after player-engine settings change."""
        self._player_restart_at = time.monotonic() + 1.5

    # --------------------------------------------------------------- actions

    def call_api(self, fn: Callable, then: Callable | None = None,
                 refresh: bool = True, describe: str = "",
                 on_error: Callable[[BaseException], None] | None = None) -> None:
        """Run an API call on a worker; surface errors; optionally re-poll.

        Player-state commands (refresh=True) go through the single-threaded
        control pool so they reach Spotify in press order; on failure we poll
        immediately to resync the optimistic UI with reality.
        """
        def done(result, error):
            if error is not None:
                if isinstance(error, NeedsLogin):
                    self.notify("session expired — run `zpotify auth`", error=True)
                elif isinstance(error, ApiError):
                    self.notify(f"{describe or 'api'}: {error.message}", error=True)
                else:
                    self.notify(f"{describe or 'api'}: {error}", error=True)
                if refresh:
                    self._next_poll = 0.0  # resync optimistic UI with reality
                if on_error is not None:
                    on_error(error)
                return
            if then is not None:
                then(result)
            if refresh:
                self._next_poll = time.monotonic() + 0.35  # let Spotify settle
        pool = self.control if refresh else self.workers
        pool.submit(fn, done)

    def play_tracks(self, uris: list[str] | None = None, context_uri: str | None = None,
                    offset_position: int | None = None, offset_uri: str | None = None) -> None:
        if self.device_id is None:
            self.notify("player device not ready yet", error=True)
            return
        self._mark_action()
        self.call_api(
            lambda: self.api.play(device_id=self.device_id, uris=uris,
                                  context_uri=context_uri,
                                  offset_position=offset_position,
                                  offset_uri=offset_uri),
            describe="play",
            then=lambda _: self._begin_fade_in(track_change=True))

    def _mark_action(self) -> None:
        """An optimistic local mutation happened; stale polls must be dropped."""
        self._action_at = time.monotonic()

    def toggle_play(self) -> None:
        state = self.playback
        if state is not None and state.is_playing:
            if self.config.pause_fade:
                self.audio.fade_to(0.0, 0.12)  # masks Connect latency, no click
            # optimistic: freeze the clock and flip the icon immediately
            state.progress_ms = self.progress_ms()
            state.is_playing = False
            self._poll_at = time.monotonic()
            self._mark_action()
            self.call_api(self.api.pause, describe="pause")
        elif state is not None and state.track is not None:
            state.is_playing = True
            self._poll_at = time.monotonic()
            self._mark_action()
            # raise the envelope NOW — if it only happened on API success, a
            # failed/raced resume would leave the audio silenced forever
            self._begin_fade_in(track_change=False)
            self.call_api(lambda: self.api.play(device_id=self.device_id),
                          describe="resume")
        else:
            self.notify("nothing to resume — pick a track (/ to search)")

    def _optimistic_track_change(self) -> None:
        """Reset the progress bar immediately; the poll fills in the new track."""
        state = self.playback
        if state is not None and state.track is not None:
            state.progress_ms = 0
            state.is_playing = True
            self._poll_at = time.monotonic()
        self._mark_action()

    def next_track(self) -> None:
        self._optimistic_track_change()
        self.call_api(self.api.next_track, describe="next",
                      then=lambda _: self.audio.flush())

    def previous_track(self) -> None:
        self._optimistic_track_change()
        self.call_api(self.api.previous_track, describe="previous",
                      then=lambda _: self.audio.flush())

    def _seek_to(self, target_ms: int) -> None:
        state = self.playback
        if state is None or state.track is None:
            return
        # optimistic: the progress bar jumps on the keypress/click
        state.progress_ms = target_ms
        self._poll_at = time.monotonic()
        self._mark_action()
        self.call_api(lambda: self.api.seek(target_ms), describe="seek",
                      then=lambda _: self.audio.flush())

    def seek_relative(self, delta_ms: int) -> None:
        state = self.playback
        if state is None or state.track is None:
            return
        self._seek_to(min(max(0, self.progress_ms() + delta_ms),
                          state.track.duration_ms - 1000))

    def seek_fraction(self, fraction: float) -> None:
        state = self.playback
        if state is None or state.track is None:
            return
        self._seek_to(int(state.track.duration_ms * min(max(fraction, 0.0), 1.0)))

    def adjust_volume(self, delta: float) -> None:
        self.audio.volume = min(1.0, max(0.0, self.audio.volume + delta))
        self.notify(f"volume {int(self.audio.volume * 100)}%")

    def toggle_shuffle(self) -> None:
        state = not (self.playback.shuffle if self.playback else False)
        if self.playback is not None:
            self.playback.shuffle = state  # optimistic
            self._mark_action()
        self.call_api(lambda: self.api.set_shuffle(state),
                      describe="shuffle")

    def cycle_repeat(self) -> None:
        order = ["off", "context", "track"]
        current = self.playback.repeat if self.playback else "off"
        mode = order[(order.index(current) + 1) % 3] if current in order else "off"
        if self.playback is not None:
            self.playback.repeat = mode  # optimistic
            self._mark_action()
        self.call_api(lambda: self.api.set_repeat(mode), describe="repeat")

    def cycle_visualizer(self) -> None:
        order = ["spectrum", "wave", "off"]
        self.visualizer = order[(order.index(self.visualizer) + 1) % 3] \
            if self.visualizer in order else "spectrum"
        self.notify(f"visualizer: {self.visualizer}")

    def switch_view(self, index: int) -> None:
        if 0 <= index < len(self.views) and index != self.view_index:
            self.view_index = index
            self.views[index].on_show(self)

    def notify(self, message: str, error: bool = False) -> None:
        self._status = message
        self._status_error = error
        self._status_until = time.monotonic() + 4.0

    def progress_ms(self) -> int:
        """Interpolated track position between polls."""
        state = self.playback
        if state is None or state.track is None:
            return 0
        progress = state.progress_ms
        if state.is_playing:
            progress += int((time.monotonic() - self._poll_at) * 1000)
        return min(progress, state.track.duration_ms)

    def add_hit(self, x: int, y: int, w: int, h: int, handler: HitHandler) -> None:
        self._hits.append((x, y, w, h, handler))

    # ------------------------------------------------------------ event flow

    def _dispatch(self, event) -> None:
        if isinstance(event, Resize):
            self.screen.resize()
        elif isinstance(event, Key):
            self._handle_key(event)
        elif isinstance(event, Mouse):
            self._handle_mouse(event)
        elif isinstance(event, Paste):
            view = self.views[self.view_index]
            if view.wants_text:
                for ch in event.text:
                    view.handle_key(self, Key(char=ch))

    def _handle_key(self, key: Key) -> None:
        if self.quit_confirm:
            # y confirms; any other key cancels (ctrl-c still quits outright)
            self.quit_confirm = False
            if key.char in ("y", "Y") or (key.ctrl and key.char == "c"):
                self.quit_requested = True
            return
        if self.help_visible:
            self.help_visible = False
            return
        view = self.views[self.view_index]
        if view.wants_text:
            if view.handle_key(self, key):
                return
        if key.ctrl and key.char == "c":
            self.quit_requested = True
            return
        char = key.char if not view.wants_text else ""
        name = key.name
        if char == "q":
            self.quit_confirm = True
        elif name == "space":
            self.toggle_play()
        elif char == "n":
            self.next_track()
        elif char == "b":
            self.previous_track()
        elif char in ("+", "="):
            self.adjust_volume(0.05)
        elif char == "-":
            self.adjust_volume(-0.05)
        elif char == ",":
            self.seek_relative(-10_000)
        elif char == ".":
            self.seek_relative(10_000)
        elif char == "s":
            self.toggle_shuffle()
        elif char == "r":
            self.cycle_repeat()
        elif char == "v":
            self.cycle_visualizer()
        elif char == "?":
            self.help_visible = True
        elif char == "/":
            self.switch_view(1)
            from zpotify.ui.views.search import SearchView
            view = self.views[1]
            assert isinstance(view, SearchView)
            view.focus_input()
        elif char and char in "1234567":
            self.switch_view(int(char) - 1)
        elif not view.wants_text:
            view.handle_key(self, key)

    def _handle_mouse(self, mouse: Mouse) -> None:
        if self.quit_confirm and mouse.kind == "press":
            self.quit_confirm = False  # clicking anywhere cancels
            return
        if self.help_visible and mouse.kind == "press":
            self.help_visible = False
            return
        for x, y, w, h, handler in self._hits:
            if x <= mouse.x < x + w and y <= mouse.y < y + h:
                handler(mouse)
                return

    # -------------------------------------------------------------- rendering

    def _tick(self, now: float) -> None:
        if self.device_id is not None and now >= self._next_poll:
            self._next_poll = now + POLL_INTERVAL
            self.workers.submit(
                self.api.playback,
                lambda result, error, t0=now: self._on_playback(result, error, t0))
        if self.visualizer == "spectrum":
            self.analyzer.update(self.audio.latest(2048))
        if self._player_restart_at is not None and now >= self._player_restart_at:
            self._player_restart_at = None
            self.notify("restarting player…")
            self.workers.submit(self._restart_librespot, None)
        self._drive_track_fade()
        # Self-heal: if Spotify says we're playing but the fade envelope is
        # parked at 0 (a pause/resume race), ramp it back up — silence must
        # never be a permanent state while playback is active.
        state = self.playback
        if state is not None and state.is_playing and not self._fade_out_started \
                and self.audio.env_target == 0.0:
            self.audio.fade_to(1.0, 0.15)

    def _drive_track_fade(self) -> None:
        """Fade out approaching the end of the track; recover on track repeat."""
        fade = self.config.fade_seconds
        state = self.playback
        if fade <= 0 or state is None or state.track is None:
            return
        remaining = state.track.duration_ms - self.progress_ms()
        if state.is_playing and not self._fade_out_started \
                and 0 < remaining <= fade * 1000:
            self._fade_out_started = True
            self.audio.fade_to(0.0, remaining / 1000)
        elif self._fade_out_started and self.progress_ms() < 5000:
            # new/repeated track began without a librespot Loading event
            self._begin_fade_in(track_change=True)

    def _on_playback(self, result, error, requested_at: float = float("inf")) -> None:
        if error is not None:
            if isinstance(error, NeedsLogin):
                self.notify("session expired — run `zpotify auth`", error=True)
            return
        if requested_at < self._action_at:
            return  # snapshot predates a local optimistic action: stale
        self.playback = result
        self._poll_at = time.monotonic()

    def _render(self) -> None:
        screen = self.screen
        self._hits.clear()
        cols, rows = screen.size
        screen.clear(theme.BASE)
        self._render_header(cols)
        body_h = rows - 1 - 3
        if body_h > 0:
            self.views[self.view_index].render(self, screen, 0, 1, cols, body_h)
        self._render_player_bar(0, rows - 3, cols)
        if self.help_visible:
            self._render_help(cols, rows)
        if self._librespot_auth_url:
            self._render_librespot_auth(cols, rows)
        if self.quit_confirm:
            self._render_quit_confirm(cols, rows)
        screen.present()

    def _render_header(self, cols: int) -> None:
        screen = self.screen
        screen.fill(0, 0, cols, 1, " ", theme.TAB_INACTIVE)
        labels = [f" {i + 1} {v.name} " for i, v in enumerate(self.views)]
        ranges = tabs(screen, 0, 0, labels, self.view_index,
                      theme.TAB_ACTIVE, theme.TAB_INACTIVE)
        for i, (x0, x1) in enumerate(ranges):
            index = i
            self.add_hit(x0, 0, x1 - x0, 1,
                         lambda m, index=index: m.kind == "press" and self.switch_view(index))
        if self.user_name:
            label = f" {self.user_name} "
            screen.put(cols - len(label), 0, label, theme.TAB_INACTIVE)

    def _render_player_bar(self, x: int, y: int, w: int) -> None:
        screen = self.screen
        state = self.playback
        screen.hline(x, y, w, "─", theme.FAINT)
        track: Track | None = state.track if state else None
        if track is not None:
            title = f" {track.name}  "
            screen.put(x + 1, y + 1, title[:w - 2], theme.TITLE)
            artist = f"{track.artist} — {track.album}"
            ax = x + 1 + min(len(title), w - 2)
            screen.put(ax, y + 1, artist[:max(0, w - ax - 1)], theme.DIM)
        else:
            screen.put(x + 1, y + 1, "nothing playing — press / to search",
                       theme.DIM)
        # transport cluster (clickable)
        is_playing = bool(state and state.is_playing)
        buttons = [
            ("⏮ ", self.previous_track),
            ("⏸ " if is_playing else "▶ ", self.toggle_play),
            ("⏭ ", self.next_track),
        ]
        bx = x + 1
        by = y + 2
        for label, action in buttons:
            style = theme.ACCENT_BOLD if label.startswith(("▶", "⏸")) else theme.BASE
            screen.put(bx, by, label, style)
            self.add_hit(bx, by, len(label), 1,
                         lambda m, action=action: m.kind == "press" and action())
            bx += len(label) + 1

        # mode flags
        if state:
            flags = []
            flags.append(("shuffle", state.shuffle, self.toggle_shuffle))
            flags.append((f"repeat:{state.repeat}", state.repeat != "off", self.cycle_repeat))
            for label, on, action in flags:
                text = f"[{label}]"
                screen.put(bx, by, text, theme.ACCENT if on else theme.FAINT)
                self.add_hit(bx, by, len(text), 1,
                             lambda m, action=action: m.kind == "press" and action())
                bx += len(text) + 1

        # time + progress
        duration = track.duration_ms if track else 0
        progress = self.progress_ms()
        time_label = f"{_mmss(progress)} / {_mmss(duration)}" if track else ""
        # volume readout (scroll wheel target)
        vol_label = f"vol {int(self.audio.volume * 100):3d}%"
        vx = x + w - len(vol_label) - 1
        screen.put(vx, by, vol_label, theme.DIM)
        self.add_hit(vx, by, len(vol_label), 1, self._volume_wheel)
        if time_label:
            tx = vx - len(time_label) - 2
            if tx > bx:
                screen.put(tx, by, time_label, theme.DIM)

        # the progress bar sits on the rule line (y), full width
        bar_x, bar_w = x + 1, w - 2
        fraction = (progress / duration) if duration else 0.0
        ProgressBar.render(screen, bar_x, y, bar_w, fraction,
                           theme.BAR_DONE, theme.BAR_TODO)
        self.add_hit(bar_x, y, bar_w, 1, lambda m: (
            m.kind == "press" and self.seek_fraction(ProgressBar.hit(m.x - bar_x, bar_w))))

        # status message overrides the middle of the controls line
        if self._status and time.monotonic() < self._status_until:
            style = theme.ERROR if self._status_error else theme.ACCENT
            msg = f" {self._status} "
            mx = x + max(bx + 1, (w - len(msg)) // 2)
            if mx + len(msg) < vx:
                screen.put(mx, by, msg, style)

    def _volume_wheel(self, mouse: Mouse) -> None:
        if mouse.kind == "scroll_up":
            self.adjust_volume(0.05)
        elif mouse.kind == "scroll_down":
            self.adjust_volume(-0.05)

    def _render_help(self, cols: int, rows: int) -> None:
        lines = [
            ("space", "play / pause"), ("n / b", "next / previous track"),
            (", / .", "seek -10s / +10s"), ("+ / -", "volume"),
            ("s", "toggle shuffle"), ("r", "cycle repeat"),
            ("v", "visualizer: spectrum / wave / off"),
            ("/", "search"), ("1-7", "switch view (7 = settings)"),
            ("j k / arrows", "navigate lists"), ("enter", "play selection"),
            ("f", "save/unsave track (library)"), ("q", "quit (y confirms)"),
            ("", ""), ("mouse", "click rows, tabs, buttons; wheel scrolls;"),
            ("", "click the top progress bar to seek"),
        ]
        w = 56
        h = len(lines) + 4
        x = (cols - w) // 2
        y = (rows - h) // 2
        self.screen.fill(x, y, w, h, " ", theme.BASE)
        self.screen.box(x, y, w, h, theme.ACCENT, title=" help ")
        for i, (keys, desc) in enumerate(lines):
            self.screen.put(x + 3, y + 2 + i, f"{keys:>14}", theme.ACCENT_BOLD)
            self.screen.put(x + 19, y + 2 + i, desc, theme.BASE)

    def _render_quit_confirm(self, cols: int, rows: int) -> None:
        w, h = 40, 5
        x = (cols - w) // 2
        y = (rows - h) // 2
        self.screen.fill(x, y, w, h, " ", theme.BASE)
        self.screen.box(x, y, w, h, theme.ACCENT_BOLD, title=" quit? ")
        self.screen.put(x + 3, y + 2, "press ", theme.BASE)
        self.screen.put(x + 9, y + 2, "y", theme.ACCENT_BOLD)
        self.screen.put(x + 10, y + 2, " to quit — any other key stays",
                        theme.DIM)

    def _render_librespot_auth(self, cols: int, rows: int) -> None:
        url = self._librespot_auth_url or ""
        w = min(max(len(url) + 6, 40), cols - 4)
        h = 7
        x = (cols - w) // 2
        y = (rows - h) // 2
        self.screen.fill(x, y, w, h, " ", theme.BASE)
        self.screen.box(x, y, w, h, theme.ERROR, title=" librespot sign-in required ")
        self.screen.put(x + 3, y + 2, "librespot needs a one-time browser sign-in:",
                        theme.BASE)
        self.screen.put(x + 3, y + 4, url[:w - 6], theme.ACCENT)


def _mmss(ms: int) -> str:
    seconds = max(0, ms // 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"
