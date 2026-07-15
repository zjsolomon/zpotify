"""Spectrum analysis for the real-audio visualizers.

:class:`SpectrumAnalyzer` turns a window of mono samples into a log-spaced,
dB-normalized bar array with classic visualizer ballistics (fast attack, slow
decay) and slow-falling peak holds.  :func:`waveform` downsamples samples to
min/max amplitude columns for the oscilloscope view.  Pure numpy, no scipy.
"""

from __future__ import annotations

import numpy as np


class SpectrumAnalyzer:
    """Log-spaced FFT bar analyzer with attack/decay smoothing and peak-hold."""

    def __init__(
        self,
        n_bins: int = 48,
        samplerate: int = 44100,
        window_size: int = 2048,
        fmin: float = 40.0,
        fmax: float = 16000.0,
        attack: float = 0.55,
        decay: float = 0.12,
    ) -> None:
        self.n_bins = n_bins
        self.samplerate = samplerate
        self.window_size = window_size
        self.attack = attack
        self.decay = decay

        self._window = np.hanning(window_size).astype(np.float32)
        # Normalize dB range: -60..0 dB -> 0..1, where 0 dB is a full-scale
        # (unit-amplitude) sine.  Reference power = (coherent gain)^2, so we
        # divide bin power by this before taking dB.
        self._db_floor = -60.0
        self._ref_power = float((self._window.sum() / 2.0) ** 2)

        # Geometric edges mapped to rfft bin indices, each bin >= 1 fft bin wide.
        n_fft_bins = window_size // 2 + 1
        hz_per_bin = samplerate / window_size
        fmax = min(fmax, samplerate / 2.0)
        ratio = (fmax / fmin) ** (1.0 / n_bins)
        edges = fmin * ratio ** np.arange(n_bins + 1)
        idx = np.round(edges / hz_per_bin).astype(np.int64)
        idx = np.clip(idx, 1, n_fft_bins - 1)
        for i in range(1, len(idx)):
            if idx[i] <= idx[i - 1]:
                idx[i] = idx[i - 1] + 1
        idx = np.clip(idx, 1, n_fft_bins)
        self._edges = idx

        self.bars = np.zeros(n_bins, dtype=np.float32)
        self.peaks = np.zeros(n_bins, dtype=np.float32)
        self._peak_hold = np.zeros(n_bins, dtype=np.int32)

        self._peak_hold_frames = 15
        self._peak_gravity = np.float32(0.02)

    def update(self, samples: np.ndarray) -> np.ndarray:
        """Fold a window of mono ``samples`` into :attr:`bars` and return it."""
        buf = np.asarray(samples, dtype=np.float32)
        if buf.shape[0] < self.window_size:
            buf = np.concatenate(
                (np.zeros(self.window_size - buf.shape[0], dtype=np.float32), buf)
            )
        else:
            buf = buf[-self.window_size:]

        spectrum = np.fft.rfft(buf * self._window)
        power = (spectrum.real ** 2 + spectrum.imag ** 2)

        target = np.empty(self.n_bins, dtype=np.float32)
        for b in range(self.n_bins):
            lo, hi = self._edges[b], self._edges[b + 1]
            mean_power = power[lo:hi].mean()
            db = 10.0 * np.log10(mean_power / self._ref_power + 1e-12)
            target[b] = np.clip((db - self._db_floor) / (0.0 - self._db_floor), 0.0, 1.0)

        rising = target > self.bars
        self.bars = np.where(
            rising,
            self.bars + (target - self.bars) * self.attack,
            self.bars + (target - self.bars) * self.decay,
        ).astype(np.float32)

        # Peak-hold: latch new peaks, hold, then fall under gravity.
        above = self.bars >= self.peaks
        self.peaks = np.where(above, self.bars, self.peaks).astype(np.float32)
        self._peak_hold = np.where(above, self._peak_hold_frames, self._peak_hold - 1)
        falling = self._peak_hold <= 0
        self.peaks = np.where(
            falling, np.maximum(self.peaks - self._peak_gravity, 0.0), self.peaks
        ).astype(np.float32)

        return self.bars


def waveform(samples: np.ndarray, width: int) -> np.ndarray:
    """Downsample ``samples`` to ``width`` columns of peak amplitude in [0, 1].

    Each column reports the larger of ``|min|`` and ``|max|`` over its slice, so
    the oscilloscope shows the envelope of the signal.
    """
    if width <= 0:
        return np.zeros(0, dtype=np.float32)
    buf = np.asarray(samples, dtype=np.float32)
    out = np.zeros(width, dtype=np.float32)
    if buf.shape[0] == 0:
        return out
    edges = np.linspace(0, buf.shape[0], width + 1).astype(np.int64)
    for c in range(width):
        lo, hi = edges[c], edges[c + 1]
        if hi <= lo:
            hi = lo + 1
        seg = buf[lo:hi]
        out[c] = max(abs(float(seg.min())), abs(float(seg.max())))
    return np.clip(out, 0.0, 1.0)
