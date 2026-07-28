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


def scope_name_for(attr):
    """Map packed cell attr -> scope name, or None for default.

    Does NOT register the scope with Sublime. Pure mapping for tests/renderer.
    """
    if attr == 0:
        return None
    fg = attr & ATTR_FG_MASK
    bg = (attr & ATTR_BG_MASK) >> BG_SHIFT
    if attr & REVERSE:
        fg, bg = bg, fg
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
