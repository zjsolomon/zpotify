"""Hand-rolled terminal engine: no curses, no TUI framework.

Modules:
  style.py   — Style dataclass (truecolor fg/bg + attributes)
  events.py  — Key / Mouse / Resize / Tick event types
  screen.py  — raw mode, alt screen, styled-cell frame buffer, diff renderer
  input.py   — escape-sequence key decoder, SGR-1006 mouse decoder, SIGWINCH
  widgets.py — ListView, ProgressBar, TextInput, tabs/status-bar helpers
"""
