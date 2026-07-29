"""Terminal color model: xterm-256 palette, packed attrs, scope names.

Pure Python — no Sublime imports. Safe to unit-test outside ST.
"""
from functools import lru_cache

# xterm 256 palette. ANSI 0-15 use Terminus "true_black" vivid values.
_ANSI16_RGB = [
    (0x00, 0x00, 0x00), (0xFF, 0x00, 0x00), (0x00, 0xFF, 0x00), (0xFF, 0xFF, 0x00),
    (0x00, 0x00, 0xFF), (0xFF, 0x00, 0xFF), (0x00, 0xFF, 0xFF), (0xFF, 0xFF, 0xFF),
    (0x80, 0x80, 0x80), (0xFF, 0x00, 0x00), (0x00, 0xFF, 0x00), (0xFF, 0xFF, 0x00),
    (0x00, 0x00, 0xFF), (0xFF, 0x00, 0xFF), (0x00, 0xFF, 0xFF), (0xFF, 0xFF, 0xFF),
]


def xterm256_rgb(n):
    """xterm 256-colour index -> (r, g, b)."""
    if n < 16:
        return _ANSI16_RGB[n]
    if n >= 232:
        v = 8 + (n - 232) * 10
        return (v, v, v)
    m = n - 16
    r, g, b = m // 36, (m // 6) % 6, m % 6
    return (0 if r == 0 else 55 + r * 40,
            0 if g == 0 else 55 + g * 40,
            0 if b == 0 else 55 + b * 40)


XTERM256_RGB = [xterm256_rgb(i) for i in range(256)]


@lru_cache(maxsize=10000)
def quantize256(r, g, b):
    """Nearest of the xterm 256 palette by squared distance -> 0..255."""
    best, best_d = 0, 1 << 30
    for i, (pr, pg, pb) in enumerate(XTERM256_RGB):
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best, best_d = i, d
    return best


# Packed attr bit layout
FG_SHIFT, BG_SHIFT = 0, 9
ATTR_FG_MASK = 0x1FF
ATTR_BG_MASK = 0x1FF << BG_SHIFT
BOLD = 1 << 18
REVERSE = 1 << 19
FAINT = 1 << 20


def pack_attr(fg=0, bg=0, flags=0):
    return (fg << FG_SHIFT) | (bg << BG_SHIFT) | flags


BG_LUMA_THRESHOLD = 100

ANSI16_HEX = [
    "#000000", "#FF0000", "#00FF00", "#FFFF00",
    "#0000FF", "#FF00FF", "#00FFFF", "#FFFFFF",
    "#808080", "#FF0000", "#00FF00", "#FFFF00",
    "#0000FF", "#FF00FF", "#00FFFF", "#FFFFFF",
]


def xterm_hex(i):
    if i < 16:
        return ANSI16_HEX[i]
    if i >= 232:
        v = 8 + (i - 232) * 10
        return "#%02X%02X%02X" % (v, v, v)
    n = i - 16
    r, g, b = n // 36, (n // 6) % 6, n % 6
    return "#%02X%02X%02X" % (
        0 if r == 0 else 55 + r * 40,
        0 if g == 0 else 55 + g * 40,
        0 if b == 0 else 55 + b * 40,
    )


HEX = [None] + [xterm_hex(i) for i in range(256)]


# 1-based palette ids used when SGR reverse hits "default" fg/bg (0).
# Matches typical xterm: default fg ≈ white, default bg ≈ black.
_DEFAULT_FG_ID = 16  # xterm 15 bright white
_DEFAULT_BG_ID = 1   # xterm 0 black

# Near-black fill used instead of #000000 so ST still treats the region as
# having a real background (Terminus-style). Below this channel-sum, dark
# palette backgrounds collapse to the near-black fill.
BG_NEAR_BLACK = "#000001"
DEFAULT_FG_HEX = "#FFFFFF"


def hex_channel_sum(h):
    """Sum of R+G+B for a #RRGGBB string; 0 if malformed."""
    if not h or len(h) < 7 or h[0] != "#":
        return 0
    try:
        return int(h[1:3], 16) + int(h[3:5], 16) + int(h[5:7], 16)
    except ValueError:
        return 0


def hex_luma(h):
    """Perceptual luma 0..255 for #RRGGBB."""
    if not h or len(h) < 7 or h[0] != "#":
        return 0
    try:
        r, g, b = int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)
    except ValueError:
        return 0
    return (299 * r + 587 * g + 114 * b) // 1000


def ensure_contrast(fg_hex, bg_hex, min_delta=48):
    """Return a readable fg against bg_hex (white or black if too close).

    Grok and other dark TUIs often paint ANSI black (or near-black truecolor)
    on a dark panel. After we collapse dark backgrounds to near-black for the
    ST region bug, that becomes black-on-black — typed text vanishes.
    """
    fg_hex = fg_hex or DEFAULT_FG_HEX
    bg_hex = bg_hex or BG_NEAR_BLACK
    if abs(hex_luma(fg_hex) - hex_luma(bg_hex)) >= min_delta:
        return fg_hex
    return DEFAULT_FG_HEX if hex_luma(bg_hex) < 128 else "#000000"


def scheme_colors_for(fg_id, bg_id):
    """Map 1-based palette ids (0 = default) -> (fg_hex, bg_fill_hex).

    Pure helper used by the Sublime scheme registrar and unit tests.
    """
    fh = HEX[fg_id] if 0 < fg_id < len(HEX) else None
    bh = HEX[bg_id] if 0 < bg_id < len(HEX) else None
    if bh is None:
        bg_fill = BG_NEAR_BLACK
    else:
        bg_fill = bh if hex_channel_sum(bh) >= BG_LUMA_THRESHOLD else BG_NEAR_BLACK
    fg_fill = ensure_contrast(fh or DEFAULT_FG_HEX, bg_fill)
    return fg_fill, bg_fill


def scope_name_for(attr):
    """Map packed cell attr -> scope name, or None for default.

    Does NOT register the scope with Sublime. Pure mapping for tests/renderer.

    Important: SGR reverse (bit REVERSE) with *default* colors must still
    produce a scope. Swapping 0↔0 stays 0, and an early return of None made
    TUI block cursors (reverse space on default colours) invisible.
    """
    if attr == 0:
        return None
    fg = attr & ATTR_FG_MASK
    bg = (attr & ATTR_BG_MASK) >> BG_SHIFT
    if attr & REVERSE:
        # Resolve defaults *before* swap so reverse-of-default becomes
        # black-on-white (visible block cursor), not (0,0) → no region.
        f = fg if fg else _DEFAULT_FG_ID
        b = bg if bg else _DEFAULT_BG_ID
        fg, bg = b, f
    if attr & FAINT:
        r, g, b = XTERM256_RGB[fg - 1] if fg else (255, 255, 255)
        fg = quantize256(r // 2, g // 2, b // 2) + 1
    if fg == 0 and bg == 0:
        return None
    return f"ai.fb.{fg}.{bg}"


def rstrip_cells(cells):
    """Drop trailing (space, default-attr) cells."""
    end = len(cells)
    while end > 0 and cells[end - 1] == (" ", 0):
        end -= 1
    return cells[:end]
