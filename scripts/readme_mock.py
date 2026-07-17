"""Build a faithful ASCII mock of the zpotify UI for the README.

Mirrors the real render math in ui/app.py and ui/views/now_playing.py:
tabs row, track info at x+3, wordmark at top-right, spectrum bars,
UP NEXT box bottom-right, 3-row player bar with progress on the rule.

Usage: uv run python scripts/readme_mock.py
Paste the output into the README's code block whenever the UI layout
changes, so the diagram never drifts from what the app actually draws.
"""
from zpotify.ui import wordmark

W, H = 92, 25
grid = [[" "] * W for _ in range(H)]


def put(x, y, s):
    for i, ch in enumerate(s):
        if 0 <= x + i < W:
            grid[y][x + i] = ch


# ── header: tabs + username (app._render_header) ──────────────────────
views = ["now playing", "search", "playlists", "library", "queue",
         "devices", "settings"]
cx = 0
for i, v in enumerate(views):
    text = f" {i + 1} {v} "
    put(cx, 0, text)
    cx += len(text)
user = " ziedjohn "
put(W - len(user), 0, user)

# ── body: track info (x+3, rows cy..cy+2) ─────────────────────────────
put(3, 2, "Levels")
put(3, 3, "Avicii")
put(3, 4, "Levels")

# wordmark at x + w - WIDTH - 3, y + 1
wx, wy = W - wordmark.WIDTH - 3, 2
for cy in range(wordmark.HEIGHT):
    top = wordmark._GRID[cy * 2]
    bot = (wordmark._GRID[cy * 2 + 1]
           if cy * 2 + 1 < wordmark.FONT_ROWS else [0] * wordmark.WIDTH)
    for gx in range(wordmark.WIDTH):
        t, b = top[gx], bot[gx]
        if not t and not b:
            continue
        grid[wy + cy][wx + gx] = "█" if (t and b) else ("▀" if t else "▄")

# ── visualizer: spectrum bars (viz area rows 8..20, 1-wide bars, gap) ──
EIGHTHS = " ▁▂▃▄▅▆▇█"
VIZ_TOP, VIZ_BOT = 8, 20
# heights in eighths of a cell for bars at cols 2,4,6,... (left of the box)
heights = [22, 34, 51, 78, 64, 88, 71, 55, 40, 62, 83, 90, 74, 58, 47,
           66, 79, 53, 38, 27, 18, 11]
peaks = {3: 12, 5: 12, 11: 12, 16: 11}  # bar index -> peak row height
for i, hh in enumerate(heights):
    x = 2 + i * 2
    full, rem = divmod(hh, 8)
    for row in range(full):
        grid[VIZ_BOT - row][x] = "█"
    if rem:
        grid[VIZ_BOT - full][x] = EIGHTHS[rem]
    pk = peaks.get(i)
    if pk and pk > full:
        grid[VIZ_BOT - pk][x] = "─"

# ── UP NEXT box, bottom-right of the viz area ──────────────────────────
songs = [
    ("Wake Me Up", "Avicii"),
    ("The Nights", "Avicii"),
    ("Hey Brother", "Avicii"),
    ("Waiting For Love", "Avicii"),
    ("Red Lights", "Tiësto"),
    ("The Business", "Tiësto"),
    ("The Motto", "Tiësto, Ava Max"),
    ("Are You With Me", "Lost Frequencies"),
    ("Reality", "Lost Frequencies"),
    ("Where Are You Now", "Lost Frequencies"),
]
box_w, rows = 44, len(songs)
box_h = rows + 2
bx = 2 + (W - 4) - box_w          # x+2, w-4 rect, right-aligned
by = VIZ_TOP + (VIZ_BOT - VIZ_TOP + 1) - box_h
put(bx - 1, by, " ")               # backdrop col (blank anyway)
put(bx, by, "┌" + "─" * (box_w - 2) + "┐")
put(bx + 2, by, " UP NEXT ")
inner_w = box_w - 4
for i, (name, artist) in enumerate(songs):
    text = (f"{i + 1:>2} " + f"{name} — {artist}")[:inner_w].ljust(inner_w)
    put(bx, by + 1 + i, "│ " + text + " │")
put(bx, by + rows + 1, "└" + "─" * (box_w - 2) + "┘")

# ── player bar: rule+progress, title line, controls line ──────────────
bar_y = H - 3
put(0, bar_y, "─" * W)
dur, pos = 199, 84                 # Levels 3:19, at 1:24
frac = pos / dur
bar_x, bar_w = 1, W - 2
total = round(frac * bar_w * 8)
full, rem = divmod(total, 8)
prog = "█" * full + ("▏▎▍▌▋▊▉"[rem - 1] if rem else "")
prog = prog + "░" * (bar_w - len(prog))
put(bar_x, bar_y, prog)

put(1, bar_y + 1, " Levels  Avicii — Levels")
put(1, bar_y + 2, "⏮  ⏸  ⏭  [shuffle] [repeat:off]")
vol = "vol  80%"
put(W - len(vol) - 1, bar_y + 2, vol)
t = "1:24 / 3:19"
put(W - len(vol) - 1 - len(t) - 2, bar_y + 2, t)

print("\n".join("".join(r).rstrip() for r in grid))
