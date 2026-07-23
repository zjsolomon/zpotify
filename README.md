<img src="https://raw.githubusercontent.com/zjsolomon/zpotify/main/Assets/banner.png" alt="zpotify" width="640">

Spotify, entirely in your terminal. The official Spotify app never opens.

A from-scratch TUI client: hand-rolled terminal engine (no curses, no TUI
framework), its own OAuth PKCE flow, a raw stdlib Spotify Web API client, and
a real audio visualizer driven by an FFT of the PCM actually playing through
your speakers. Runtime dependencies: `numpy` and `sounddevice`. That's it.

```
 1 now playing  2 search  3 playlists  4 library  5 queue  6 devices  7 settings   ziedjohn

   Levels                                                                 █   ▀ ▄▀▀
   Avicii                                               ▀▀▀█▀ █▀▀▀▄ ▄▀▀▀▄ █▀▀ █ █▀▀ █   █
   Levels                                                ▄▀   █▄▄▄▀ █   █ █   █ █   ▀▄▄▄█
                                                        ▀▀▀▀▀ █      ▀▀▀   ▀▀ ▀ ▀       █
                                                              ▀                      ▀▀▀

        ─   ─           ─
                        ▂         ─           ┌─ UP NEXT ────────────────────────────────┐
            █         ▃ █                     │  1 Wake Me Up — Avicii                   │
        ▆   █         █ █ ▂       ▇           │  2 The Nights — Avicii                   │
        █   █ ▇       █ █ █     ▂ █           │  3 Hey Brother — Avicii                  │
        █ █ █ █     ▆ █ █ █ ▂   █ █           │  4 Waiting For Love — Avicii             │
      ▃ █ █ █ █ ▇   █ █ █ █ █   █ █ ▅         │  5 Red Lights — Tiësto                   │
      █ █ █ █ █ █   █ █ █ █ █ ▇ █ █ █         │  6 The Business — Tiësto                 │
    ▂ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ ▆       │  7 The Motto — Tiësto, Ava Max           │
    █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ ▃     │  8 Are You With Me — Lost Frequencies    │
  ▆ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ ▂   │  9 Reality — Lost Frequencies            │
  █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ ▃ │ 10 Where Are You Now — Lost Frequencies  │
  █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ █ └──────────────────────────────────────────┘

─██████████████████████████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░─
  Levels  Avicii — Levels
 ⏮  ⏸  ⏭  [shuffle] [repeat:off]                                      1:24 / 3:19  vol  80%
```

## How it works

zpotify runs [librespot](https://github.com/librespot-org/librespot) as a
subprocess that registers on your account as a Spotify Connect device — used
strictly as an audio faucet. Control (search, playlists, play/pause/seek)
goes through the Web API with a hand-written PKCE OAuth flow; the raw PCM
flows through zpotify, which plays it on your system output and FFTs it for
the visualizer. Everything you see and interact with is this codebase.

## Requirements

- **Spotify Premium** (Spotify requirement for playback outside official apps)
- macOS with [Homebrew](https://brew.sh): `brew install librespot`
- [uv](https://docs.astral.sh/uv/)
- A free Spotify developer app — the first-run wizard walks you through it

## Install & run

Install once as a global command (an isolated venv plus a `zpotify` shim in
`~/.local/bin`):

```sh
uv tool install zpotify    # or: pipx install zpotify
```

Then from anywhere:

```sh
zpotify          # first run launches the setup wizard
zpotify doctor   # health check when something's off
zpotify auth     # redo the Spotify login
```

Or try it without installing: `uvx zpotify`.

**From source / development:** `uv tool install --editable /path/to/zpotify`
(`--editable` links `src/`, so code changes apply without reinstalling; inside
the repo, `uv run zpotify` works too).

**→ Full setup walkthrough, key reference, and troubleshooting: [docs/USER_GUIDE.md](docs/USER_GUIDE.md)**

## Keys

`space` play/pause · `n`/`b` next/prev · `,`/`.` seek ±10s · `+`/`-` volume ·
`/` floating search from anywhere (`enter` searches, `/` or `esc` closes) ·
`1-7` views (7 = settings) · `tab`/`shift+tab` cycle tabs · `j`/`k`/`h`/`l` vim
navigation in views ·
`enter` play · `a` queue · `s` shuffle · `r` repeat · `v` visualizer · `?` help ·
`q` quit — plus full mouse support (click rows/tabs/buttons, scroll wheel,
click the progress bar to seek).

## Crossfade

Settings (`7`) has **crossfade** (off by default, 1–12 s), which blends each
track's ending into the start of the next. It applies only when a track
advances naturally — skips, picks and seeks always cut clean — and the
boundary is anchored to librespot's own end-of-track event, so the blend
starts exactly where the outgoing track really ends rather than at a guess.

## Themes

Settings (`7`) includes an accent **theme** — 12 colors (green default, plus
cyan, blue, teal, lime, yellow, orange, red, pink, magenta, purple, white)
that re-skin the tabs, bars, highlights, and visualizer live.

## Project layout

```
src/zpotify/
  term/       terminal engine: raw mode, diff renderer, key/mouse decoding, widgets
  auth.py     OAuth 2.0 PKCE against accounts.spotify.com (stdlib only)
  api.py      Spotify Web API client (urllib), models in models.py
  player/     librespot subprocess, PCM ring buffer → sounddevice, FFT analysis
  ui/         event loop, seven views, player bar, overlays, theming
tests/        140+ unit tests: input decoding, rendering, API parsing, audio, FFT
```

## Development

```sh
uv run pytest -q        # test suite
```

## License

[MIT](LICENSE)
