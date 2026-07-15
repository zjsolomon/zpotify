# zpotify

Spotify, entirely in your terminal. A from-scratch TUI client — hand-rolled
terminal engine (no curses, no TUI framework), own OAuth PKCE flow, raw
Spotify Web API client, and a real audio visualizer driven by an FFT of the
PCM stream actually playing through your speakers.

The official Spotify app never needs to be opened.

## How it works

```
zpotify (Python) ──HTTPS──▶ Spotify Web API        (search, playlists, control)
     ▲
     │ raw PCM (s16le 44.1kHz stereo)
librespot --backend pipe                            (Spotify Connect device)
     └──▶ zpotify plays it via sounddevice + FFTs it for the visualizer
```

librespot is used strictly as an audio faucet; everything you see and interact
with is this codebase. Dependencies: `numpy`, `sounddevice`. That's it.

## Requirements

- Spotify **Premium**
- `brew install librespot`
- A free app at [developer.spotify.com](https://developer.spotify.com/dashboard)
  with redirect URI `http://127.0.0.1:8898/callback` (the first-run wizard
  walks you through it)

## Run

```sh
uv run zpotify          # main app (first run launches the setup wizard)
uv run zpotify auth     # (re)run login only
uv run zpotify doctor   # check librespot, audio output, credentials
```

## Keys

`space` play/pause · `n` next · `b` prev · `,`/`.` seek ±10s · `+`/`-` volume ·
`/` search · `1-6` views · `j`/`k` navigate · `enter` play · `a` add to queue ·
`f` unsave (library) · `s` shuffle · `r` repeat · `v` visualizer mode ·
`R` refresh (queue/devices) · `?` help · `q` quit — plus full mouse support
(click rows/tabs/buttons, scroll wheel, click the progress bar to seek).
