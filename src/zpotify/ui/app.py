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
from zpotify.radio import REFILL_BELOW, Station
from zpotify.term.events import Key, Mouse, Paste, Resize
from zpotify.term.screen import Screen
from zpotify.term.input import InputReader
from zpotify.term.widgets import ProgressBar, TextInput, tabs
from zpotify.ui import theme
from zpotify.ui.workers import WorkerPool

FRAME = 1 / 30
POLL_INTERVAL = 2.0
ESC_TIMEOUT = 0.025
SESSION_SAVE_INTERVAL = 5.0
# Crossfade adoption tuning (see _classify_change / _resolve_pending_playback).
_NEAR_END_MS = 10_000    # inside this much of a track's end, a change is a race
_SEEK_JUMP_MS = 4_000    # clock movement beyond this is a remote seek
_PARK_GRACE = 3.0        # parked with no boundary in flight: cut over after this
_PARK_FAILSAFE = 12.0    # absolute cap (plus the fade) before forcing adoption

HitHandler = Callable[[Mouse], None]


def choose_stage(local: dict | None, remote: tuple[Track, float] | None) -> dict | None:
    """Pick what to stage at startup: zpotify's own saved session or Spotify's
    account-wide history — whichever is newer. Local wins ties because history
    omits the track a session was interrupted in the middle of."""
    local_at = (local or {}).get("saved_at", 0.0)
    remote_at = remote[1] if remote else 0.0
    if local and local.get("track") and local_at >= remote_at:
        return {"kind": "local", **local}
    if remote is not None:
        return {"kind": "remote", "track": remote[0].to_dict(),
                "progress_ms": 0, "context_uri": None, "up_next": []}
    return None


class App:
    def __init__(self, config: cfg.Config, auth: Auth) -> None:
        self.config = config
        theme.apply(config.theme)
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
        self.audio.set_crossfade(config.fade_seconds)
        self.librespot = self._make_librespot()

        from zpotify.ui.views import (DevicesView, LibraryView, NowPlayingView,
                                      PlaylistsView, QueueView, SearchView,
                                      SettingsView)
        self.views = [NowPlayingView(), SearchView(), PlaylistsView(),
                      LibraryView(), QueueView(), DevicesView(), SettingsView()]
        self.view_index = 0

        self.playback: PlaybackState | None = None
        # "Staged" start: nothing was playing anywhere, so the freshest of
        # (zpotify's own saved session, account-wide last played) is shown
        # paused, ready for space to start it.
        self._staged = False
        self._stage_attempted = False
        self._staged_session: dict | None = None
        self._session_saved_at = 0.0
        # Radio. `station` is the live generator (see radio.py — Spotify's own
        # recommendations API is closed to personal apps, so zpotify builds
        # stations from the listener's library ranked by audio similarity). It
        # fills an empty staged queue locally, and pushes into Spotify's real
        # queue once a session is live.
        self.up_next_is_radio = False
        self.station: Station | None = None
        self._station_busy = False
        self._radio_ids: set[str] = set()  # tracks this station put in the queue
        self._radio_tries = 0
        self._radio_retry_at: float | None = None
        # Live-queue top-up: UP NEXT should never sit (nearly) empty, so short
        # queues are padded from a passive station. Kept per seed artist so
        # the 15s queue poll doesn't rebuild constantly.
        self._filler_station: Station | None = None
        self._filler_key: str | None = None
        self._filler_fetching = False
        self.up_next: list[Track] = []   # queue preview for the now-playing view
        self._next_queue_poll = 0.0
        self._last_track_id: str | None = None
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
        self.search_overlay: TextInput | None = None
        self._search_overlay_rect: tuple[int, int, int, int] | None = None
        self._player_restart_at: float | None = None  # debounced settings restart
        # Optimistic UI: local actions mutate playback state immediately; any
        # poll *requested before* the action is stale and must be discarded,
        # or the icon/progress would flicker back until the next poll.
        self._action_at = 0.0
        # Crossfade: with a fade running, Spotify reports the next track
        # seconds before you can hear it. A track change seen while a boundary
        # is still in flight is parked as (state, seen_at, boundaries_played)
        # and adopted only once the blend is audible.
        self._pending_playback: tuple[PlaybackState, float, int] | None = None

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
        state = self.playback
        if (state is not None and state.device is not None
                and state.device.id == self.device_id and not self._staged):
            self._write_session_now()  # final snapshot for the next launch
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
        if event.kind == "end_of_track":
            # Mark the boundary here, not after marshalling: the engine counts
            # frames written so far, and librespot keeps feeding the *next*
            # track while a queued callback waits its turn. note_end_of_track
            # is thread-safe precisely so this can happen at observation time.
            self.audio.note_end_of_track()
        self.workers.submit(lambda: event, self._on_librespot_event)

    def _on_librespot_event(self, event: LibrespotEvent, error=None) -> None:
        if event.kind == "auth_url":
            self._librespot_auth_url = event.data.get("url")
        elif event.kind == "exit":
            self.notify("librespot exited — restarting…", error=True)
            self.workers.submit(self._restart_librespot, None)
        elif event.kind in ("playing", "paused", "stopped"):
            self._next_poll = 0.0  # confirm via API soon

    def _restart_librespot(self) -> None:
        time.sleep(1.0)
        self.librespot.stop()
        self.librespot = self._make_librespot()
        self.librespot.start()
        stream = self.librespot.stdout
        if stream is not None:
            self.audio.attach(stream)
        self.audio.flush()
        self.audio.release(0.25)
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
                    offset_position: int | None = None, offset_uri: str | None = None,
                    position_ms: int | None = None) -> None:
        if self.device_id is None:
            self.notify("player device not ready yet", error=True)
            return
        if uris is not None:
            # One malformed uri (e.g. from a degraded API response) makes
            # Spotify reject the whole payload — drop anything suspect.
            uris = [u for u in uris
                    if isinstance(u, str) and u.startswith("spotify:track:")
                    and len(u) > len("spotify:track:")]
            if not uris and context_uri is None:
                self.notify("nothing playable in that selection", error=True)
                return
            uris = uris or None
        self._mark_action()
        self.call_api(
            lambda: self.api.play(device_id=self.device_id, uris=uris,
                                  context_uri=context_uri,
                                  offset_position=offset_position,
                                  offset_uri=offset_uri,
                                  position_ms=position_ms),
            describe="play",
            # manual play: cut the old audio and ungate if we were pause-held
            then=lambda _: (self.audio.flush(), self.audio.release(0.15)))

    def _mark_action(self) -> None:
        """An optimistic local mutation happened; stale polls must be dropped."""
        self._action_at = time.monotonic()

    def toggle_play(self) -> None:
        state = self.playback
        if self._staged and state is not None and state.track is not None:
            # staged track: there is no Spotify session to resume — start a
            # fresh one that recreates the old session as closely as possible
            if self.device_id is None:
                self.notify("player device not ready yet", error=True)
                return
            session = self._staged_session or {}
            self._staged = False
            position = int(session.get("progress_ms") or 0)
            context_uri = session.get("context_uri")
            if context_uri:
                # context playback rebuilds the queue exactly like Spotify
                self.play_tracks(context_uri=context_uri,
                                 offset_uri=state.track.uri,
                                 position_ms=position)
            else:
                # no context: chain the track + saved up-next as one session
                uris = [state.track.uri] + [
                    t.uri for t in self.up_next[:10] if t.uri]
                self.play_tracks(uris=uris, position_ms=position)
            return
        if state is not None and state.is_playing:
            # local hold = instant pause; buffer is kept so resume continues
            # exactly where the listener left off
            self.audio.hold(0.12 if self.config.pause_fade else 0.0)
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
            # release NOW — if it only happened on API success, a failed or
            # raced resume would leave the audio gated forever
            self.audio.release(0.15 if self.config.pause_fade else 0.0)
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

    def skip_to_queue_index(self, index: int) -> None:
        """Play the UP NEXT row at ``index`` directly — same behavior as
        pressing enter on a search result. The rest of the visible list is
        chained behind it so listening continues down what was shown."""
        chain = [t.uri for t in self.up_next[index:index + 10] if t.uri]
        if not chain:
            return
        self._staged = False
        self.notify(f"playing: {self.up_next[index].name}")
        self.play_tracks(uris=chain)

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
        """Interpolated track position between polls (server clock)."""
        state = self.playback
        if state is None or state.track is None:
            return 0
        progress = state.progress_ms
        if state.is_playing:
            progress += int((time.monotonic() - self._poll_at) * 1000)
        return min(progress, state.track.duration_ms)

    def display_progress_ms(self) -> int:
        """Track position as *heard*: the server clock minus audio still in
        flight (ring buffer + pipe, well under a second).

        Once a crossfade boundary has played, the engine's stream-counted
        clock is exact — it starts at 0 the instant the new track becomes
        audible — so it beats the latency estimate.
        """
        state = self.playback
        if state is None or state.track is None:
            return 0
        if not state.is_playing:
            return self.progress_ms()
        crossed = self.audio.crossed_ms
        if crossed is not None:
            return min(crossed, state.track.duration_ms)
        lag = int(self.audio.latency * 1000)
        return max(0, self.progress_ms() - lag)

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
            if self.search_overlay is not None:
                for ch in event.text:
                    self.search_overlay.handle_key(Key(char=ch))
                return
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
        if self.search_overlay is not None:
            self._handle_search_overlay_key(key)
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
        elif char == "x":
            self.start_radio()
        elif char == "?":
            self.help_visible = True
        elif char == "/":
            self.search_overlay = TextInput()
        elif name == "tab":
            self.switch_view((self.view_index + 1) % len(self.views))
        elif name == "backtab":  # shift+tab
            self.switch_view((self.view_index - 1) % len(self.views))
        elif char and char in "1234567":
            self.switch_view(int(char) - 1)
        elif not view.wants_text:
            view.handle_key(self, key)

    def _handle_search_overlay_key(self, key: Key) -> None:
        overlay = self.search_overlay
        if overlay is None:
            return
        if key.char == "/" or key.name == "esc":
            self.search_overlay = None
            return
        if key.name == "enter":
            text = overlay.value
            self.search_overlay = None
            if text.strip():
                from zpotify.ui.views.search import SearchView
                view = self.views[1]
                assert isinstance(view, SearchView)
                view.query.value = text
                view.query.cursor = len(text)
                view._search(self)
                view.focused = False
                self.switch_view(1)
            return
        overlay.handle_key(key)

    def _handle_mouse(self, mouse: Mouse) -> None:
        if self.quit_confirm and mouse.kind == "press":
            self.quit_confirm = False  # clicking anywhere cancels
            return
        if self.search_overlay is not None:
            if mouse.kind == "press":
                rect = self._search_overlay_rect
                inside = (rect is not None and rect[0] <= mouse.x < rect[0] + rect[2]
                         and rect[1] <= mouse.y < rect[1] + rect[3])
                if not inside:
                    self.search_overlay = None
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
        if self.device_id is not None and now >= self._next_queue_poll:
            self._next_queue_poll = now + 15.0
            self.workers.submit(self.api.queue, self._on_queue)
        if (self._radio_retry_at is not None and now >= self._radio_retry_at
                and self._staged and not self.up_next):
            self._radio_retry_at = None
            self.workers.submit(self._fetch_radio, self._on_radio)
        # Keep a live station ahead of the player without waiting for the
        # 15s queue poll to notice its tracks running out.
        if (self.station is not None and not self._staged
                and self.device_id is not None
                and self.radio_pending() < REFILL_BELOW):
            self._pump_station()
        # Adopt a held-back track change once its boundary is audible (the
        # crossfade has begun), or cut over if no boundary materializes.
        self._resolve_pending_playback(now)
        if self._player_restart_at is not None and now >= self._player_restart_at:
            self._player_restart_at = None
            self.notify("restarting player…")
            self.workers.submit(self._restart_librespot, None)
        # Self-heal: if Spotify says we're playing but our output is gated
        # (a pause/resume race), ramp it back up — silence must never be a
        # permanent state while playback is active. A held engine is
        # intentional; leave it alone.
        state = self.playback
        if state is not None and state.is_playing \
                and self.audio.env_target == 0.0 \
                and not self.audio.held:
            self.audio.fade_to(1.0, 0.15)

    def _on_playback(self, result, error, requested_at: float = float("inf")) -> None:
        if error is not None:
            if isinstance(error, NeedsLogin):
                self.notify("session expired — run `zpotify auth`", error=True)
            return
        if requested_at < self._action_at:
            return  # snapshot predates a local optimistic action: stale
        if result is None:
            if self._staged:
                return  # keep the staged track on screen
            self.playback = None
            self._poll_at = time.monotonic()
            if not self._stage_attempted and self.device_id is not None:
                self._stage_attempted = True
                self.workers.submit(self._fetch_stage_candidate, self._on_stage)
            return
        now = time.monotonic()
        verdict = self._classify_change(result)
        if verdict == "park":
            # Natural advance mid-crossfade: keep showing the outgoing track
            # until the blend is actually audible.
            self._pending_playback = (result, now, self.audio.boundaries_played)
            return
        if verdict == "flush":
            # A skip or seek from another device. Without this the big standing
            # buffer would keep playing the old track for many seconds.
            self.audio.flush()
        self._adopt_playback(result, now)

    def _classify_change(self, result: PlaybackState) -> str | None:
        """Decide how to treat an unsolicited poll: park, flush, or adopt.

        Only meaningful with crossfade on — at fade 0 every change is adopted
        immediately, exactly as before the feature existed.
        """
        if self.config.fade_seconds <= 0:
            return None
        current = self.playback
        if current is None or current.track is None or result.track is None:
            return None

        if result.track.id != current.track.id:
            if self.audio.transition_pending:
                return "park"
            # The poll can beat librespot's end_of_track event. Near the end of
            # the outgoing track, assume that race rather than a remote skip;
            # _tick cuts over if no boundary ever shows up.
            remaining = current.track.duration_ms - self.progress_ms()
            if 0 <= remaining <= _NEAR_END_MS:
                return "park"
            return "flush"

        # Same track, but the clock jumped: a seek from another device. A
        # boundary in flight means repeat-one restarting, which must not flush.
        if self.audio.transition_pending:
            return None
        if abs(result.progress_ms - self.progress_ms()) > _SEEK_JUMP_MS:
            return "flush"
        return None

    def _resolve_pending_playback(self, now: float) -> None:
        """Adopt, drop, or time out a parked track change."""
        if self._pending_playback is None:
            return
        result, seen_at, played_before = self._pending_playback
        if seen_at < self._action_at:
            self._pending_playback = None  # user skipped meanwhile: stale
            return
        if self.audio.boundaries_played > played_before:
            self._pending_playback = None
            self._adopt_playback(result, now)  # the blend is audible now
            return
        if not self.audio.transition_pending and now - seen_at >= _PARK_GRACE:
            # Parked on the near-end guess, but no boundary ever arrived — it
            # was a remote skip after all.
            self._pending_playback = None
            self.audio.flush()
            self._adopt_playback(result, now)
            return
        if now - seen_at >= _PARK_FAILSAFE + self.config.fade_seconds:
            # A mark that never plays must never wedge the UI.
            self._pending_playback = None
            self.audio.flush()
            self._adopt_playback(result, now)

    def _adopt_playback(self, result: PlaybackState, poll_at: float) -> None:
        self._staged = False
        self.playback = result
        self._poll_at = poll_at
        self._maybe_save_session()
        track_id = result.track.id if result.track else None
        if track_id != self._last_track_id:
            self._last_track_id = track_id
            self.refresh_queue_soon()

    def _fetch_stage_candidate(self) -> dict | None:
        """Worker: newest of the local saved session and Spotify's history."""
        remote = None
        try:
            remote = self.api.last_played()
        except Exception:  # noqa: BLE001 — offline history is fine
            pass
        return choose_stage(cfg.read_session(), remote)

    def _on_stage(self, result, error) -> None:
        """Stage a candidate track as ready-to-play (paused, position kept)."""
        if error is not None or result is None or self.playback is not None:
            return
        track = Track.from_dict(result["track"])
        progress = int(result.get("progress_ms") or 0)
        self.playback = PlaybackState(is_playing=False, progress_ms=progress,
                                      track=track,
                                      context_uri=result.get("context_uri"))
        self.up_next = [Track.from_dict(d) for d in result.get("up_next") or []]
        self.up_next_is_radio = False
        self._staged = True
        self._staged_session = result
        self._poll_at = time.monotonic()
        self.notify(f"ready: {track.name} — space plays")
        if not self.up_next:
            # no saved queue: build a radio-style one so UP NEXT never sits
            # empty; space chains it into the real session
            self._radio_tries = 0
            self.workers.submit(self._fetch_radio, self._on_radio)

    def start_radio(self) -> None:
        """Start an endless station seeded from whatever is playing now."""
        state = self.playback
        seed = state.track if state is not None else None
        if seed is None or not seed.id:
            self.notify("play something first — radio seeds from the current track",
                        error=True)
            return
        self.station = Station(self.api, seed)
        self.up_next_is_radio = True
        self._radio_ids = set()
        # Display-only fillers are not real queue entries; drop them so UP NEXT
        # shows what the station actually queues.
        self._filler_station = None
        self._filler_key = None
        self.notify(f"radio: {self.station.label} — building…")
        self._pump_station(prime=True)

    def stop_radio(self) -> None:
        self.station = None
        self.up_next_is_radio = False
        self._radio_ids = set()

    def radio_pending(self) -> int:
        """How many of the station's own tracks are still queued ahead.

        Counting *our* tracks rather than the whole queue matters: playing
        from a playlist leaves dozens of context tracks in UP NEXT forever,
        and a station that measured those would never top itself up.
        """
        return sum(1 for t in self.up_next if t.id in self._radio_ids)

    def _pump_station(self, prime: bool = False) -> None:
        """Ask the station for more, on a worker. Safe to call every tick."""
        if self.station is None or self._station_busy:
            return
        self._station_busy = True
        self.workers.submit(
            lambda: self._station_work(prime),
            lambda result, error: self._on_station(result, error, prime))

    def _station_work(self, prime: bool = False) -> list[Track]:
        """Worker: refill the station and hand the picks to Spotify's queue.

        Pushing into the real queue (rather than keeping a private list) means
        auto-advance, crossfade and the queue view all keep working exactly as
        they do for any other playback. Queued tracks play before the current
        context resumes, so the station stays in front of it.
        """
        station = self.station
        if station is None:
            return []
        station.exclude(self.up_next)
        # Priming always queues a full batch: pressing `x` while a queue is
        # already showing has to visibly do something.
        wanted = REFILL_BELOW if prime else max(0, REFILL_BELOW - self.radio_pending())
        if station.pending < wanted:
            station.refill()
        picks = station.take(wanted)
        queued: list[Track] = []
        for track in picks:
            if not track.uri:
                continue
            try:
                self.api.add_to_queue(track.uri, device_id=self.device_id)
            except ApiError:
                break  # device went away or rate-limited; try again next tick
            queued.append(track)
        return queued

    def _on_station(self, result, error, prime: bool = False) -> None:
        self._station_busy = False
        if error is not None:
            self.notify("radio: could not reach Spotify — will retry", error=True)
            return
        self._radio_ids.update(t.id for t in result)
        if prime:
            if not result:
                self.notify("radio: found nothing to play from that track", error=True)
                self.stop_radio()
                return
            # Show the station immediately; the queue poll reconciles it.
            self.up_next = list(result)
            label = self.station.label if self.station is not None else ""
            self.notify(f"radio: {label} — {len(result)} queued, n skips into it")
        if result:
            self.refresh_queue_soon()

    def _fetch_radio(self) -> list[Track]:
        """Worker: local radio fill for a staged (not yet playing) session.

        Nothing is playing yet, so there is no real queue to push into — the
        picks are returned for UP NEXT to display and chain from.
        """
        state = self.playback
        if state is None or state.track is None:
            return []
        station = Station(self.api, state.track)
        self.station = station
        station.refill()
        return station.take(10)

    def _on_radio(self, result, error) -> None:
        if not self._staged or self.up_next:
            return  # a real queue appeared meanwhile
        if error is not None or not result:
            self._radio_tries += 1
            if self._radio_tries < 3:
                self._radio_retry_at = time.monotonic() + 20.0
            return
        self.up_next = result
        self.up_next_is_radio = True

    def _maybe_save_session(self) -> None:
        """Persist what zpotify itself is playing so a restart can restore it.

        Only when we are the active device — the phone's sessions are not
        ours to remember (Spotify's history covers those).
        """
        state = self.playback
        if (state is None or state.track is None or state.device is None
                or self.device_id is None or state.device.id != self.device_id):
            return
        now = time.monotonic()
        if now - self._session_saved_at < SESSION_SAVE_INTERVAL:
            return
        self._session_saved_at = now
        self._write_session_now()

    def _write_session_now(self) -> None:
        state = self.playback
        if state is None or state.track is None:
            return
        cfg.write_session({
            "saved_at": time.time(),
            "track": state.track.to_dict(),
            "progress_ms": self.progress_ms(),
            "context_uri": state.context_uri,
            "up_next": [t.to_dict() for t in self.up_next[:10]],
        })

    def refresh_queue_soon(self) -> None:
        self._next_queue_poll = 0.0

    def _on_queue(self, result, error) -> None:
        if self._staged:
            return  # don't clobber a staged/radio queue with the dead session's
        if error is None and isinstance(result, list):
            self.up_next = result
            # A running station feeds this very queue, so its badge stays on.
            self.up_next_is_radio = self.station is not None
            self._maybe_top_up_queue()

    def _seed_key(self, track: Track) -> str:
        return track.artists[0] if track.artists else track.name

    def _maybe_top_up_queue(self) -> None:
        """Keep UP NEXT from sitting (nearly) empty.

        With a station running this is the station's job — it pushes into
        Spotify's real queue. Otherwise a passive station seeded from the
        current track supplies display-only fillers: enter chains from them,
        but they are never pushed into Spotify's queue.
        """
        state = self.playback
        if (state is None or state.track is None or self._staged
                or len(self.up_next) >= 10 or self._filler_fetching):
            return
        if self.station is not None:
            self._pump_station()
            return
        seed = state.track
        key = self._seed_key(seed)
        if self._filler_key != key or self._filler_station is None:
            self._filler_key = key
            self._filler_station = Station(self.api, seed)
        station = self._filler_station
        station.exclude([seed, *self.up_next])
        want = 10 - len(self.up_next)
        self._filler_fetching = True
        self.workers.submit(lambda: self._filler_work(station, want),
                            self._on_filler)

    def _filler_work(self, station: Station, want: int) -> list[Track]:
        if station.pending < want:
            station.refill()
        return station.take(want)

    def _on_filler(self, result, error) -> None:
        self._filler_fetching = False
        if error is not None or not result:
            return
        self.up_next = self.up_next + result[:10 - len(self.up_next)]

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
        if self.search_overlay is not None:
            self._render_search_overlay(cols, rows)
        if self.quit_confirm:
            self._render_quit_confirm(cols, rows)
        screen.present()

    def _render_header(self, cols: int) -> None:
        screen = self.screen
        screen.fill(0, 0, cols, 1, " ", theme.TAB_INACTIVE)
        labels = [f"{i + 1} {v.name}" for i, v in enumerate(self.views)]
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

        # time + progress (as heard, not as streamed)
        duration = track.duration_ms if track else 0
        progress = self.display_progress_ms()
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
            ("x", "start radio from the current track"),
            ("/", "search from anywhere (floating box)"),
            ("1-7", "switch view (7 = settings)"),
            ("tab / shift+tab", "next / previous tab"),
            ("h / l", "back / cycle within a view (vim)"),
            ("j k / arrows", "navigate lists"), ("enter", "play selection"),
            ("↑ ↓ + enter", "pick a song from UP NEXT (now playing)"),
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

    def _render_search_overlay(self, cols: int, rows: int) -> None:
        overlay = self.search_overlay
        if overlay is None:
            return
        w = min(60, cols - 8)
        h = 5
        x = (cols - w) // 2
        y = (rows - h) // 2
        self._search_overlay_rect = (x, y, w, h)
        self.screen.fill(x, y, w, h, " ", theme.BASE)
        self.screen.box(x, y, w, h, theme.ACCENT, title=" search ")
        overlay.render(self.screen, x + 2, y + 2, w - 4, theme.INPUT_FOCUS, True)
        hint = "enter searches · / or esc closes"
        self.screen.put(x + 2, y + 3, hint[:w - 4], theme.FAINT)

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
