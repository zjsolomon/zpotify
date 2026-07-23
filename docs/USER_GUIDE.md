# zpotify user guide

Everything you need to run Spotify entirely from your terminal.

- [Requirements](#requirements)
- [One-time setup](#one-time-setup)
- [Everyday use](#everyday-use)
- [The views](#the-views)
- [Keyboard reference](#keyboard-reference)
- [Radio](#radio)
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

Colors are authored as 24-bit RGB and sent as-is to terminals that advertise
`COLORTERM=truecolor`; everything else gets the nearest xterm-256 color, which
looks slightly flatter but is always correct. Apple Terminal.app is always
treated as 256-color — it advertises truecolor but mangles the sequences.
`zpotify doctor` prints the mode it picked; override it with
`ZPOTIFY_COLOR=truecolor` or `ZPOTIFY_COLOR=256` (useful under tmux, which
hides `COLORTERM` from the app).

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

## Installing as a global command

This is how Python CLIs are normally installed — `uv tool` (or pipx) creates
an isolated environment and puts a shim on your PATH:

```sh
uv tool install --editable /path/to/zpotify
```

Now `zpotify` runs from any directory. Housekeeping:

```sh
uv tool list              # see installed tools
uv tool upgrade zpotify   # re-resolve dependencies after they change
uv tool uninstall zpotify # remove the command
```

`--editable` links the repo's `src/`, so pulling/editing code applies
immediately without reinstalling. Config always lives in `~/.config/zpotify/`
no matter where you run from. (If the project is ever published to PyPI,
anyone could install it with just `uv tool install zpotify`.)

## Everyday use

```sh
zpotify          # the app
zpotify doctor   # health check: librespot, logins, audio, Premium
zpotify auth     # redo the Spotify login only
```

On launch, if nothing is playing anywhere on your account, zpotify stages a
track paused on the now-playing screen, ready for `space`: whichever is newer
of (a) what zpotify itself was last playing — with its position, playlist
context, and up-next queue restored — or (b) your account's most recently
played track from any device, phone included. If the staged track has no saved
queue, zpotify fills **UP NEXT · RADIO** with a radio-style list built from the
track's artist (Spotify closed its real recommendations API to personal apps),
and `space` plays the track with that list chained behind it — the radio you
see is the radio you get. Otherwise press `/`, type a song or artist, `enter`
to search, arrow down to a result, `enter` again. Audio comes out of your
default output device; the Spotify app is never involved.

## The views

Switch with keys `1`–`7` or click the tabs.

| # | View | What it does |
|---|---|---|
| 1 | **now playing** | Big track info + the audio visualizer, with an **UP NEXT** box showing the next 10 queue tracks. `↑`/`↓` (or `j`/`k`) highlight a song, `enter` (or double-click) plays it directly — like picking a search result — with the rest of the list chained after it. `esc` clears the highlight. |
| 2 | **search** | Type a query, `enter` to search; `enter` plays a result, `a` queues it. The input isn't auto-focused — with no results, `enter` engages it (or press `/` from anywhere for the floating search box). |
| 3 | **playlists** | Your playlists; `enter` opens one, `enter` on a track plays it *in playlist context* (so next/shuffle work within the playlist), `esc` goes back. |
| 4 | **library** | Your liked songs; `enter` plays, `a` queues, `f` removes from library. |
| 5 | **queue** | What's coming up next (`R` refreshes). |
| 6 | **devices** | Every Spotify Connect device on your account; `enter` transfers playback to it (including back to zpotify). |
| 7 | **settings** | Edit settings in-app (`enter`/`→` next value, `←` previous, click to cycle); saved instantly to `config.json`: streaming quality (96/160/320 kbps), **crossfade** (off/1–12 s — blends each track's ending into the next, only when a track advances naturally; skips, picks and seeks always cut clean; off = plain transitions exactly as before), pause/resume fade, volume normalization, default visualizer, and the accent **theme** — 12 colors (green, cyan, blue, teal, lime, yellow, orange, red, pink, magenta, purple, white) that re-skin the tabs, bars, highlights, and visualizer live. Quality and normalization restart the player engine (~2 s blip); crossfade applies live. |

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
| `1`–`7` | switch view (7 = settings) |
| `tab` / `shift+tab` | next / previous tab |
| `h` / `l` | vim-style within views: `h` backs out of a playlist, `h`/`l` cycle a setting's value |
| `/` | open a floating search box from anywhere; `enter` searches and jumps to the search view, `/` or `esc` dismisses |
| `j` / `k` or arrows | move through lists |
| `pgup` / `pgdn`, `home` / `end` | page / jump in lists |
| `enter` | play / open selection |
| `a` | add selected track to queue |
| `f` | unsave track (library view) |
| `R` | refresh (queue & devices views) |
| `esc` | leave search input / go back from playlist tracks |
| `v` | cycle visualizer: spectrum → wave → off |
| `x` | start **radio** from the current track (see below) |
| `?` | help overlay (any key closes it) |
| `q` | quit — shows a confirmation popup; `y` quits, any other key (or a click) cancels |
| `ctrl-c` | quit immediately, no confirmation |

## Mouse reference

- **Click** a tab to switch view; click a list row to select it, click it again to play.
- **Scroll wheel** scrolls lists; scrolling over the `vol NN%` readout changes volume.
- **Click the progress bar** (the thin line above the track title) to seek.
- **Click** the `⏮ ▶ ⏭` transport buttons and the `[shuffle]` / `[repeat]` flags.

## Radio

Press `x` while something is playing to start an endless station seeded from
that track. UP NEXT switches to `UP NEXT · RADIO — <artist>`, and the station
keeps Spotify's queue topped up in the background — it never runs out, and each
refill digs further rather than repeating what it already offered.

Stations are zpotify's own work. Spotify closed its `/recommendations`
endpoint (along with related-artists, artist top-tracks and all of `/browse`)
to personal apps, so there is no "official" radio to call. Instead each station
is built from the endpoints that still answer:

- the seed artist's **genre tags**, expanded by searching those genres;
- **your own listening** — top tracks and saved library, filtered to those genres;
- **deep cuts** from the seed artist's albums.

Candidates are ranked by how closely their audio features (energy, valence,
danceability, acousticness, tempo) match the seed, then spread out so no artist
dominates and none plays twice in a row. Playing something else, or restarting
zpotify, ends the station.

One caveat worth knowing: Spotify has announced the removal of the audio-features
endpoint for personal apps. When it goes, stations lose the similarity ranking
but keep working — the mix just gets less finely sorted.

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
| "Spotify doesn't let personal API apps read playlists you don't own" | A Spotify policy for development-mode apps: track listings of playlists owned by other accounts (or editorial ones) are blocked. zpotify can still *play* them — press `enter` on the message. Your own and collaborative playlists list normally. |
| No sound but the progress bar moves | Check the volume readout (bottom right) and your macOS output device; `doctor` prints which output it found. |
| Choppy / stuttering audio | Another process may be starving the audio thread; try quitting heavy apps. File an issue with `doctor` output if it persists. |
| Terminal left in a weird state after a crash | `reset` in the shell fixes it; zpotify restores the terminal on all normal exits including ctrl-c. |

## Files on disk / resetting

Everything lives in `~/.config/zpotify/`:

| Path | Contents |
|---|---|
| `config.json` | client ID, volume, visualizer, quality, fade, theme settings |
| `tokens.json` | OAuth tokens (chmod 600) |
| `librespot/` | librespot's credential + system cache |

Full reset: `rm -rf ~/.config/zpotify` and run `uv run zpotify` again.
Log out only: `uv run zpotify auth` (re-login) or delete `tokens.json`.
