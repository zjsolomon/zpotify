"""Configuration and on-disk state for zpotify.

Everything lives under ~/.config/zpotify.bak/:
  config.json   — client id + user settings
  tokens.json   — OAuth tokens (chmod 600)
  librespot/    — librespot credential/audio cache
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("ZPOTIFY_CONFIG_DIR", "~/.config/zpotify.bak")).expanduser()
CONFIG_FILE = CONFIG_DIR / "config.json"
TOKENS_FILE = CONFIG_DIR / "tokens.json"
LIBRESPOT_CACHE_DIR = CONFIG_DIR / "librespot"

REDIRECT_PORT = 8898
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
DEVICE_NAME = "zpotify"

SCOPES = (
    "user-read-playback-state user-modify-playback-state "
    "user-read-currently-playing user-read-private "
    "playlist-read-private playlist-read-collaborative "
    "user-library-read user-library-modify "
    "user-read-recently-played user-top-read"
)


_KNOWN_KEYS = ("client_id", "volume", "visualizer", "bitrate",
               "fade_seconds", "pause_fade", "normalization")


@dataclass
class Config:
    client_id: str = ""
    volume: float = 0.8
    visualizer: str = "spectrum"  # spectrum | wave | off
    bitrate: int = 320            # 96 | 160 | 320 kbps (librespot restart)
    fade_seconds: float = 0.0     # track fade in/out; 0 = off
    pause_fade: bool = True       # short fade on pause/resume
    normalization: bool = False   # librespot volume normalisation (restart)
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_FILE.exists():
            return cls()
        data = json.loads(CONFIG_FILE.read_text())
        known = {k: data.pop(k) for k in _KNOWN_KEYS if k in data}
        return cls(**known, extra=data)

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {**{k: getattr(self, k) for k in _KNOWN_KEYS}, **self.extra}
        CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n")


def read_tokens() -> dict | None:
    if not TOKENS_FILE.exists():
        return None
    try:
        return json.loads(TOKENS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_tokens(tokens: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2) + "\n")
    TOKENS_FILE.chmod(0o600)


def clear_tokens() -> None:
    TOKENS_FILE.unlink(missing_ok=True)
