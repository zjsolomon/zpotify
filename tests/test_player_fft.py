"""Tests for the spectrum analyzer and waveform helper."""

from __future__ import annotations

import numpy as np

from zpotify.player.fft import SpectrumAnalyzer, waveform

SR = 44100
WIN = 2048


def _sine(freq: float, n: int, sr: int = SR) -> np.ndarray:
    t = np.arange(n) / sr
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


def _expected_bin(analyzer: SpectrumAnalyzer, freq: float) -> int:
    hz_per_bin = analyzer.samplerate / analyzer.window_size
    fft_idx = freq / hz_per_bin
    for b in range(analyzer.n_bins):
        if analyzer._edges[b] <= fft_idx < analyzer._edges[b + 1]:
            return b
    return analyzer.n_bins - 1


def test_sine_energy_concentrates_in_expected_bin() -> None:
    analyzer = SpectrumAnalyzer(samplerate=SR, window_size=WIN)
    samples = _sine(440.0, WIN)
    for _ in range(10):  # let attack settle
        bars = analyzer.update(samples)
    expected = _expected_bin(analyzer, 440.0)
    assert abs(int(np.argmax(bars)) - expected) <= 1


def test_silence_decays_toward_zero() -> None:
    analyzer = SpectrumAnalyzer(samplerate=SR, window_size=WIN)
    loud = _sine(440.0, WIN)
    for _ in range(10):
        analyzer.update(loud)
    assert analyzer.bars.max() > 0.1
    silence = np.zeros(WIN, dtype=np.float32)
    for _ in range(200):
        analyzer.update(silence)
    assert analyzer.bars.max() < 0.05


def test_bars_always_in_unit_range() -> None:
    analyzer = SpectrumAnalyzer(samplerate=SR, window_size=WIN)
    rng = np.random.default_rng(0)
    for _ in range(20):
        noise = rng.standard_normal(WIN).astype(np.float32)
        bars = analyzer.update(noise * 10.0)
        assert bars.min() >= 0.0
        assert bars.max() <= 1.0
        assert analyzer.peaks.min() >= 0.0
        assert analyzer.peaks.max() <= 1.0


def test_update_handles_short_input() -> None:
    analyzer = SpectrumAnalyzer(samplerate=SR, window_size=WIN)
    bars = analyzer.update(_sine(440.0, WIN // 4))
    assert bars.shape == (analyzer.n_bins,)


def test_peaks_track_and_are_at_least_bars() -> None:
    analyzer = SpectrumAnalyzer(samplerate=SR, window_size=WIN)
    loud = _sine(440.0, WIN)
    for _ in range(10):
        analyzer.update(loud)
    assert np.all(analyzer.peaks >= analyzer.bars - 1e-6)


def test_waveform_width_and_range() -> None:
    samples = _sine(220.0, SR // 10)
    wf = waveform(samples, 80)
    assert wf.shape == (80,)
    assert wf.min() >= 0.0
    assert wf.max() <= 1.0
    assert wf.max() > 0.5  # full-scale sine


def test_waveform_edge_cases() -> None:
    assert waveform(np.zeros(0, dtype=np.float32), 10).shape == (10,)
    assert waveform(_sine(220.0, 100), 0).shape == (0,)
    assert np.all(waveform(np.zeros(100, dtype=np.float32), 20) == 0.0)
