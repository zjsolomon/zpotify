"""Tests for the settings model: config persistence and value cycling."""

from __future__ import annotations

import json

from zpotify import config as cfg
from zpotify.ui.views.settings import Setting


def _tmp_config(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.json")


def test_new_fields_round_trip(monkeypatch, tmp_path) -> None:
    _tmp_config(monkeypatch, tmp_path)
    config = cfg.Config(client_id="abc")
    config.bitrate = 160
    config.fade_seconds = 5.0
    config.pause_fade = False
    config.normalization = True
    config.save()

    loaded = cfg.Config.load()
    assert loaded.bitrate == 160
    assert loaded.fade_seconds == 5.0
    assert loaded.pause_fade is False
    assert loaded.normalization is True

    raw = json.loads((tmp_path / "config.json").read_text())
    assert raw["bitrate"] == 160


def test_defaults_when_fields_absent(monkeypatch, tmp_path) -> None:
    _tmp_config(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text('{"client_id": "abc", "volume": 0.5}')
    loaded = cfg.Config.load()
    assert loaded.bitrate == 320
    assert loaded.fade_seconds == 0.0
    assert loaded.pause_fade is True
    assert loaded.normalization is False


def test_theme_round_trip(monkeypatch, tmp_path) -> None:
    _tmp_config(monkeypatch, tmp_path)
    config = cfg.Config(client_id="abc")
    config.theme = "cyan"
    config.save()
    assert cfg.Config.load().theme == "cyan"


def test_theme_default_when_absent(monkeypatch, tmp_path) -> None:
    _tmp_config(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text('{"client_id": "abc"}')
    assert cfg.Config.load().theme == "green"


def test_setting_cycle_and_display() -> None:
    state = {"v": 160}
    setting = Setting(
        label="quality", description="", needs_restart=True,
        options=[(96, "96 kbps"), (160, "160 kbps"), (320, "320 kbps")],
        get=lambda: state["v"], set=lambda v: state.__setitem__("v", v),
    )
    assert setting.current_display() == "160 kbps"
    setting.cycle(1)
    assert state["v"] == 320
    setting.cycle(1)  # wraps
    assert state["v"] == 96
    setting.cycle(-1)  # wraps backwards
    assert state["v"] == 320


def test_crossfade_setting_applies_live_without_restart() -> None:
    from types import SimpleNamespace

    from zpotify.player.audio import AudioEngine
    from zpotify.ui.views.settings import _build_settings

    config = cfg.Config(client_id="abc")
    app = SimpleNamespace(config=config, audio=AudioEngine(), visualizer="off")
    row = next(s for s in _build_settings(app) if s.label == "crossfade")
    assert not row.needs_restart                # applies live, no player blip
    assert row.current_display() == "off"
    base = app.audio._fill_limit

    row.cycle(1)                                # off -> 1 s
    assert config.fade_seconds == 1.0
    assert app.audio._fill_limit > base         # engine resized immediately
    row.cycle(-1)                               # back to off
    assert config.fade_seconds == 0.0
    assert app.audio._fill_limit == base        # exact old behavior restored


def test_setting_cycle_recovers_from_unknown_value() -> None:
    state = {"v": 999}
    setting = Setting(
        label="x", description="", options=[(1, "a"), (2, "b")],
        get=lambda: state["v"], set=lambda v: state.__setitem__("v", v),
    )
    assert setting.current_display() == "999"
    setting.cycle(1)  # unknown -> steps from index 0
    assert state["v"] == 2
