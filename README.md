# zpotify

Spotify, entirely in your terminal. The official Spotify app never opens.

A from-scratch TUI client: hand-rolled terminal engine (no curses, no TUI
framework), its own OAuth PKCE flow, a raw stdlib Spotify Web API client, and
a real audio visualizer driven by an FFT of the PCM actually playing through
your speakers. Runtime dependencies: `numpy` and `sounddevice`. That's it.

```
┌ 1 now playing │ 2 search │ 3 playlists │ 4 library │ 5 queue │ 6 devices ┐
│                                                                          │
│   Midnight City                                                          │
│   M83                                                                    │
│   Hurry Up, We're Dreaming                                               │
│                                                                          │
│      ▂▄▆█▇▅▃▂▁ ▁▂▄▆█▆▄▂ ▁▃▅▇█▇▅▃▁ ▂▄▆▇▆▄▂   ← live FFT of the audio      │
│   ▁▃▅▇███▇▅▃▁▂▄▆███▆▄▂▁▃▅▇████▇▅▃▁▂▄▆██▆▄▂                               │
│ ██████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ │
│  Midnight City  M83 — Hurry Up, We're Dreaming                           │
│  ⏮ ⏸ ⏭ [shuffle] [repeat:off]              2:04 / 4:03      vol  80%     │
└──────────────────────────────────────────────────────────────────────────┘
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

## Run

```sh
uv run zpotify          # first run launches the setup wizard
uv run zpotify doctor   # health check when something's off
uv run zpotify auth     # redo the Spotify login
uv run zpotify demo     # terminal-engine demo (no Spotify account needed)
```

**→ Full setup walkthrough, key reference, and troubleshooting: [docs/USER_GUIDE.md](docs/USER_GUIDE.md)**

## Keys

`space` play/pause · `n`/`b` next/prev · `,`/`.` seek ±10s · `+`/`-` volume ·
`/` search · `1-6` views · `j`/`k` navigate · `enter` play · `a` queue ·
`s` shuffle · `r` repeat · `v` visualizer · `?` help · `q` quit — plus full
mouse support (click rows/tabs/buttons, scroll wheel, click the progress bar
to seek).

## Project layout

```
src/zpotify/
  term/       terminal engine: raw mode, diff renderer, key/mouse decoding, widgets
  auth.py     OAuth 2.0 PKCE against accounts.spotify.com (stdlib only)
  api.py      Spotify Web API client (urllib), models in models.py
  player/     librespot subprocess, PCM ring buffer → sounddevice, FFT analysis
  ui/         event loop, six views, player bar, theming
tests/        89 unit tests: input decoding, rendering, API parsing, audio, FFT
```

## Development

```sh
uv run pytest -q        # test suite
uv run zpotify demo     # exercise the terminal engine interactively
```

## License

[MIT](LICENSE)
