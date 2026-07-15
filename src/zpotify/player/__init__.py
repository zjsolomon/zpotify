"""Audio pipeline: librespot subprocess -> PCM ring buffer -> speakers + FFT.

Modules:
  librespot.py — subprocess lifecycle (--backend pipe), credential cache, restart
  audio.py     — PCM reader thread, ring buffer, sounddevice output, volume
  fft.py       — spectrum analysis (Hann window, rfft, log bins, smoothing)
"""
