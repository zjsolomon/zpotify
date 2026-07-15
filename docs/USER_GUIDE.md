# zpotify user guide

Everything you need to run Spotify entirely from your terminal.

- [Requirements](#requirements)
- [One-time setup](#one-time-setup)
- [Everyday use](#everyday-use)
- [The views](#the-views)
- [Keyboard reference](#keyboard-reference)
- [Mouse reference](#mouse-reference)
- [Visualizers](#visualizers)
- [How playback works](#how-playback-works)
- [Troubleshooting](#troubleshooting)
- [Files on disk / resetting](#files-on-disk--resetting)

## Requirements

| What | Why |
|---|---|
| **Spotify Premium** | Spotify only allows playback outside its official apps for Premium accounts. |
| **librespot** (`brew install librespot`) | The Spotify Connect engine zpotify uses as its audio source. |
| **uv** | Runs the project (`uv run zpotify`). |
| A terminal with truecolor + mouse support | iTerm2, Ghostty, WezTerm, Kitty, Terminal.app, most modern emulators. |

## One-time setup

Run the app; the wizard drives everything:

```sh
uv run zpotify
```

1. **Create your Spotify developer app** (free, takes a minute):
   - Open <https://developer.spotify.com/dashboard> and log in.
   - *Create app* — name and description can be anything (e.g. "zpotify").
   - Under **Redirect URIs**, add exactly:
     ```
     http://127.0.0.1:8898/callback
     ```
   - Tick the **Web API** checkbox and save.
   - Copy the app's **Client ID** and paste it into the wizard prompt.
2. **Spotify login** — your browser opens an authorization page; approve it.
   zpotify receives the tokens on a local callback server; nothing leaves your
   machine except the OAuth exchange with Spotify itself.
3. **Player sign-in** — librespot does its own one-time browser approval so it
   can register as a Spotify Connect device. This may complete silently if
   your browser is already logged in to Spotify.

After this, `uv run zpotify` goes straight into the app.

## Everyday use

```sh
uv run zpotify          # the app
uv run zpotify doctor   # health check: librespot, logins, audio, Premium
uv run zpotify auth     # redo the Spotify login only
uv run zpotify demo     # terminal-engine demo screen (no Spotify needed)
```

Start something playing: press `/`, type a song or artist, `enter` to search,
arrow down to a result, `enter` again. Audio comes out of your default output
device; the Spotify app is never involved.

## The views

Switch with keys `1`–`6` or click the tabs.

| # | View | What it does |
|---|---|---|
| 1 | **now playing** | Big track info + the audio visualizer. |
| 2 | **search** | Type a query, `enter` to search; `enter` plays a result, `a` queues it. |
| 3 | **playlists** | Your playlists; `enter` opens one, `enter` on a track plays it *in playlist context* (so next/shuffle work within the playlist), `esc` goes back. |
| 4 | **library** | Your liked songs; `enter` plays, `a` queues, `f` removes from library. |
| 5 | **queue** | What's coming up next (`R` refreshes). |
| 6 | **devices** | Every Spotify Connect device on your account; `enter` transfers playback to it (including back to zpotify). |

## Keyboard reference

**Playback**

| Key | Action |
|---|---|
| `space` | play / pause |
| `n` / `b` | next / previous track |
| `,` / `.` | seek −10s / +10s |
| `+` / `-` | volume up / down (local, doesn't touch other devices) |
| `s` | toggle shuffle |
| `r` | cycle repeat: off → context → track |

**Navigation & app**

| Key | Action |
|---|---|
| `1`–`6` | switch view |
| `/` | jump to search and focus the input |
| `j` / `k` or arrows | move through lists |
| `pgup` / `pgdn`, `home` / `end` | page / jump in lists |
| `enter` | play / open selection |
| `a` | add selected track to queue |
| `f` | unsave track (library view) |
| `R` | refresh (queue & devices views) |
| `esc` | leave search input / go back from playlist tracks |
| `v` | cycle visualizer: spectrum → wave → off |
| `?` | help overlay (any key closes it) |
| `q` / `ctrl-c` | quit |

## Mouse reference

- **Click** a tab to switch view; click a list row to select it, click it again to play.
- **Scroll wheel** scrolls lists; scrolling over the `vol NN%` readout changes volume.
- **Click the progress bar** (the thin line above the track title) to seek.
- **Click** the `⏮ ▶ ⏭` transport buttons and the `[shuffle]` / `[repeat]` flags.

## Visualizers

Both modes analyze the PCM audio actually coming out of your speakers —
there's no faking (Spotify removed its audio-analysis API in 2024, so zpotify
runs its own FFT on the live stream).

- **spectrum** — 48 log-spaced frequency bins, 40 Hz–16 kHz, with peak-hold
  markers and attack/decay ballistics.
- **wave** — a mirrored oscilloscope of the recent waveform.
- **off** — just the track info.

## How playback works

```
┌─────────┐  Web API (HTTPS)   ┌──────────────┐
│ zpotify │ ─────────────────▶ │   Spotify    │  search, playlists, control
│  (TUI)  │                    └──────┬───────┘
│         │                           │ encrypted Ogg (Connect protocol)
│  audio  │   raw PCM (pipe)   ┌──────▼───────┐
│  + FFT  │ ◀───────────────── │  librespot   │  runs as "zpotify" device
└─────────┘                    └──────────────┘
```

zpotify launches librespot as a subprocess registered on your account as a
Spotify Connect device named **zpotify**. Play/pause/seek/etc. go through the
Web API; the audio itself flows through zpotify, which plays it via your
system output and feeds the visualizer. Playback pacing comes from the audio
device itself (backpressure through the pipe), buffered ~0.3s for responsive
controls.

## Troubleshooting

Run `uv run zpotify doctor` first — it checks every link in the chain and
tells you which one is broken.

| Symptom | Fix |
|---|---|
| `INVALID_CLIENT: Invalid redirect URI` in the browser | The redirect URI on your dashboard app isn't exactly `http://127.0.0.1:8898/callback`. |
| "player device not ready yet" | librespot hasn't appeared in Connect yet — give it a few seconds; check the devices view (`6`). If it never appears, `uv run zpotify doctor`. |
| "session expired — run `zpotify auth`" | Spotify expired your refresh token (they rotate these aggressively since 2026). `uv run zpotify auth` re-logs you in. |
| 403 errors on playback control | Your account isn't Premium, or the track is unavailable in your market. |
| No sound but the progress bar moves | Check the volume readout (bottom right) and your macOS output device; `doctor` prints which output it found. |
| Choppy / stuttering audio | Another process may be starving the audio thread; try quitting heavy apps. File an issue with `doctor` output if it persists. |
| Terminal left in a weird state after a crash | `reset` in the shell fixes it; zpotify restores the terminal on all normal exits including ctrl-c. |

## Files on disk / resetting

Everything lives in `~/.config/zpotify.bak/`:

| Path | Contents |
|---|---|
| `config.json` | client ID, volume, visualizer choice |
| `tokens.json` | OAuth tokens (chmod 600) |
| `librespot/` | librespot's credential + system cache |

Full reset: `rm -rf ~/.config/zpotify.bak` and run `uv run zpotify` again.
Log out only: `uv run zpotify auth` (re-login) or delete `tokens.json`.
