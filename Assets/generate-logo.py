#!/usr/bin/env python3
"""Generate the zpotify graphics, in the zwisp house style: an 8-bit LED
equalizer (discrete cells lit from the base, faint ghost cells above, sharp
corners) on a near-black tile, plus a pixel wordmark banner.

zpotify's twist on the palette: the tip hues lean green/teal (Spotify
identity) and the i-dot accent is Spotify green. The wordmark glyphs are
imported from zpotify.ui.wordmark — the same pixels the TUI draws.

Run:  uv run python Assets/generate-logo.py Assets
Pure stdlib (zlib); no Pillow needed.
"""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from zpotify.ui.wordmark import FONT_ROWS, GLYPHS, WORD  # noqa: E402

# ---- palette ----------------------------------------------------------------
TILE = (6, 6, 8)            # near-black background tile
LIT = (242, 243, 250)       # lit LED cell (near-white)
GHOST = (32, 32, 38)        # unlit ghost cell
GREEN = (30, 215, 96)       # Spotify green — the i-dot accent

# Bar heights (lit cells, 1..ROWS) — zpotify's own equalizer snapshot.
HEIGHTS = [3, 5, 2, 4, 5, 3, 5, 2, 4]
BARS = len(HEIGHTS)
ROWS = 5

# Tip hues: green-leaning, in the zwisp soft-pastel register.
TIPS = [
    (87, 240, 203),   # teal
    (110, 235, 131),  # green
    (155, 240, 107),  # lime
    (87, 240, 203),   # teal
    (30, 215, 96),    # spotify green
    (244, 228, 107),  # yellow
    (110, 235, 131),  # green
    (87, 240, 203),   # teal
    (155, 240, 107),  # lime
]

BAR_GAP = 0.5
ROW_GAP = 0.5


# ---- framebuffer ------------------------------------------------------------
def new_fb(w, h):
    return [bytearray(w * 4) for _ in range(h)]


def set_px(fb, w, h, x, y, rgb):
    if 0 <= x < w and 0 <= y < h:
        o = x * 4
        fb[y][o:o + 4] = bytes((rgb[0], rgb[1], rgb[2], 255))


def fill_rect(fb, w, h, x0, y0, x1, y1, rgb):
    xa, xb = int(round(x0)), int(round(x1))
    ya, yb = int(round(y0)), int(round(y1))
    for y in range(max(0, ya), min(h, yb)):
        for x in range(max(0, xa), min(w, xb)):
            o = x * 4
            fb[y][o:o + 4] = bytes((rgb[0], rgb[1], rgb[2], 255))


def fill_rounded_rect(fb, w, h, x0, y0, x1, y1, r, rgb):
    for y in range(int(y0), int(y1) + 1):
        for x in range(int(x0), int(x1) + 1):
            dx = (x0 + r) - x if x < x0 + r else (x - (x1 - r) if x > x1 - r else 0)
            dy = (y0 + r) - y if y < y0 + r else (y - (y1 - r) if y > y1 - r else 0)
            if dx * dx + dy * dy <= r * r:
                set_px(fb, w, h, x, y, rgb)


def draw_equalizer(fb, w, h, bx, by, bw, bh):
    bar_w = bw / (BARS + (BARS - 1) * BAR_GAP)
    gap_x = bar_w * BAR_GAP
    cell_h = bh / (ROWS + (ROWS - 1) * ROW_GAP)
    gap_y = cell_h * ROW_GAP
    baseline = by + bh
    for i in range(BARS):
        x0 = bx + i * (bar_w + gap_x)
        lit = HEIGHTS[i]
        for row in range(ROWS):
            y1 = baseline - row * (cell_h + gap_y)
            y0 = y1 - cell_h
            color = (TIPS[i] if row == lit - 1 else LIT) if row < lit else GHOST
            fill_rect(fb, w, h, x0, y0, x0 + bar_w, y1, color)


# ---- wordmark (glyphs shared with the TUI) ----------------------------------
def wordmark_dims(cell):
    widths = [len(GLYPHS[c][2]) for c in WORD]
    total = sum(widths) + (len(WORD) - 1)
    return total * cell, FONT_ROWS * cell


def draw_wordmark(fb, w, h, ox, oy, cell):
    cx = ox
    for c in WORD:
        g = GLYPHS[c]
        gw = len(g[2])
        for ry in range(FONT_ROWS):
            row = g[ry] if ry < len(g) else ""
            for rx in range(gw):
                if rx < len(row) and row[rx] == "#":
                    color = GREEN if (c == "i" and ry == 0) else LIT
                    fill_rect(fb, w, h,
                              cx + rx * cell, oy + ry * cell,
                              cx + (rx + 1) * cell, oy + (ry + 1) * cell, color)
        cx += (gw + 1) * cell


# ---- compositions -----------------------------------------------------------
def render_icon(size):
    fb = new_fb(size, size)
    r = int(size * 0.2)
    fill_rounded_rect(fb, size, size, 0, 0, size - 1, size - 1, r, TILE)
    bw, bh = size * 0.64, size * 0.50
    draw_equalizer(fb, size, size, (size - bw) / 2, (size - bh) / 2, bw, bh)
    return size, size, fb


def render_banner():
    cell = 26
    ww, wh = wordmark_dims(cell)
    pad = 66
    eq_h = int(wh * 0.92)
    eq_w = int(eq_h * 1.5)
    gap = 74
    W = pad + eq_w + gap + ww + pad
    H = wh + pad * 2
    fb = new_fb(W, H)
    fill_rounded_rect(fb, W, H, 0, 0, W - 1, H - 1, int(H * 0.14), TILE)
    draw_equalizer(fb, W, H, pad, (H - eq_h) / 2, eq_w, eq_h)
    draw_wordmark(fb, W, H, pad + eq_w + gap, (H - wh) / 2, cell)
    return W, H, fb


# ---- PNG --------------------------------------------------------------------
def write_png(path, w, h, fb):
    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data +
                struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))
    raw = bytearray()
    for row in fb:
        raw += b"\x00" + row
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(__file__)
    for name, px in (("logo.png", 512), ("icon-1024.png", 1024)):
        w, h, fb = render_icon(px)
        write_png(os.path.join(out_dir, name), w, h, fb)
    w, h, fb = render_banner()
    write_png(os.path.join(out_dir, "banner.png"), w, h, fb)
    print("generated zpotify assets in", out_dir)


if __name__ == "__main__":
    main()
