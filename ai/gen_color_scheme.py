#!/usr/bin/env python3
"""Generate ai_terminal.sublime-color-scheme.

Claude's TUI emits *truecolor* SGR sequences (``38;2;r;g;b``), not 16-colour
codes. The parser quantizes every colour down to the xterm 256 palette
(216-cube + 24-step gray ramp + 16 ANSI) and maps each cell to a combined
``ai.fb.<fg>.<bg>`` scope (1-based xterm indices, 0 = default). This generator
emits the full 257x257 matrix so any cell has an exact scope.

Palette = the Terminus ``true_black`` theme (themes/true_black.json): pure
#000000 background, #FFFFFF default foreground, vivid ANSI primaries. The
xterm cube (16-231) and gray ramp (232-255) are standard xterm.

**CRITICAL -- ST add_regions scope colour swap (bit us 2026-07-03):**
``view.add_regions(scope=...)`` does NOT colour the text foreground the way
syntax highlighting does. Empirically (tested on a scratch view with Mariana
+ built-in scopes AND custom scopes): when a scope defines only a foreground,
add_regions uses that foreground as the region **fill** and the text falls
back to the global background -- "coloured backgrounds, uncoloured text,"
the exact symptom the user reported ("colors are background instead of
foreground"). The swap only happens when one of fg/bg is missing; when
**both** foreground and a solid background are defined, text = foreground and
fill = background (standard, no swap). ``#00000001``-alpha backgrounds do NOT
work in this ST build (style_for_scope strips the alpha), so the fill must be
a solid ``#000000``.

So EVERY rule here sets BOTH keys: ``foreground`` = the text colour (when
fg != 0), ``background`` = the cell's bg colour when that bg is visibly
brighter than the pure-black chat backdrop, otherwise ``#000001`` (off-by-one
from the view's #000000 global background -- ST collapses a rule background
that EQUALS the global background to None, which re-triggers the swap;
#000001 is preserved by style_for_scope while being visually
indistinguishable from pure black, so the fill is invisible for near-black
bgs).

Why the bg is now selectively honoured (reversing a prior decision): the
chat/output area paints its backdrop with near-black fields
(truecolor 4;4;4 / 20;20;20 / 12;12;12 ...) which quantize to xterm hexes
at or near #000000 -- those still get the invisible #000001 fill, so the
chat area stays pure black per the user's directive ("where there is text,
there should just be text on a black background, no deviating from that").
Genuine UI highlights -- e.g. the ctrl-p / command-palette selected row at
truecolor (250,178,131) quantizing to xterm peach -- get a real coloured
fill, so their dark-on-light fg stops rendering black-on-black (the bug
where the selected item was invisible). The luminance threshold gates
which bgs "count" as a highlight vs. a black-ish backdrop; tune
``_BG_LUMA_THRESHOLD`` if a future highlight falls below it and renders
invisible. The parser still tracks bg and honours reverse (swapping
fg/bg before mapping) so reverse video of explicit colours now also
renders correctly.

This is a scheme-only change: the parser's ``ai.fb.<fg>.<bg>`` scope format is
unchanged, so no plugin reload is required for palette iteration -- reloading
just the color scheme on the live view takes effect immediately (toggling the
scheme path away and back forces a real disk reload; see the scheme-reload
gotcha in the design memory).

Run:  python ai/gen_color_scheme.py
"""
import json
import os


def _c(x):
    return 0 if x == 0 else 55 + x * 40


# ANSI 0-15: Terminus true_black vivid values (must match ai_terminal.py's
# _ANSI16_RGB). true_black defines no distinct brights, so 8 = bright black
# (#808080) and 9-15 repeat 1-7.
_ANSI16 = [
    "#000000", "#FF0000", "#00FF00", "#FFFF00",
    "#0000FF", "#FF00FF", "#00FFFF", "#FFFFFF",
    "#808080", "#FF0000", "#00FF00", "#FFFF00",
    "#0000FF", "#FF00FF", "#00FFFF", "#FFFFFF",
]


def xterm_hex(i):
    """xterm 256-colour index -> #RRGGBB. 0-15 = true_black ANSI, 16-231 = cube,
    232-255 = gray ramp."""
    if i < 16:
        return _ANSI16[i]
    if i >= 232:
        v = 8 + (i - 232) * 10
        return "#%02X%02X%02X" % (v, v, v)
    n = i - 16
    r, g, b = n // 36, (n // 6) % 6, n % 6
    return "#%02X%02X%02X" % (_c(r), _c(g), _c(b))


# _HEX[id] for id 0..256 (0 = default -> None, inherits global #FFFFFF).
_HEX = [None] + [xterm_hex(i) for i in range(256)]

rules = []


def rule(scope, **kw):
    rules.append(dict(scope=scope, **kw))


# Scope names: ai.fb.<fg>.<bg>  (fg, bg in 0..256; 0 = default). The parser
# emits the combined ``ai.fb.<fg>.<bg>`` scope per cell and this generator
# emits the full 257x257 = 66049-rule matrix so every cell has an exact
# matching rule. Background is HONOURED selectively: bgs bright enough to
# look visibly distinct from the pure-black chat backdrop (luminance sum
# >= _BG_LUMA_THRESHOLD) get a real coloured fill so TUI highlights
# (selection bars, panel borders, ...) render; near-black bgs (the chat
# backdrop fields 4;4;4 / 20;20;20 / ...) get the invisible #000001 fill
# so the chat area stays pure black per the user's directive. fg=0 is
# omitted (inherits the global #FFFFFF); bg=0 maps to the invisible
# #000001 (no fill requested). The parser still swaps fg/bg for reverse
# BEFORE emitting the scope, so reverse now renders on the swapped bg.

# Luminance (r+g+b) cutoff on the quantized bg hex. Raising this tightens
# "what counts as a highlight"; lowering it fills more dark greys. 100
# catches truecolor >=40;40;40 (#282828, sum 120) and brighter while
# leaving the chat fields 4;4;4 / 20;20;20 (sum 12 / 60) on the invisible
# fill. The ctrl-p selection bar (truecolor 250,178,131 -> xterm peach
# #ffb387, sum 569) is far above this and renders solid.
_BG_LUMA_THRESHOLD = 100

for bg in range(257):
    bh = _HEX[bg]
    if bh is None:
        bg_fill = "#000001"
    else:
        bsum = int(bh[1:3], 16) + int(bh[3:5], 16) + int(bh[5:7], 16)
        bg_fill = bh if bsum >= _BG_LUMA_THRESHOLD else "#000001"
    for fg in range(257):
        fh = _HEX[fg]
        kw = {"background": bg_fill}
        if fh:
            kw["foreground"] = fh
        rule(f"ai.fb.{fg}.{bg}", **kw)

scheme = {
    "name": "AI Terminal",
    "variables": {},
    "globals": {
        "background": "#000000",
        "foreground": "#FFFFFF",
        "caret": "#FFFFFF",
        "selection": "#444444",
        "line_highlight": "#0a0a0a",
        "gutter": "#000000",
        "gutter_foreground": "#808080",
    },
    "rules": rules,
}

out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "ai_terminal.sublime-color-scheme")
with open(out, "w", encoding="utf-8") as f:
    # Compact (no indent) -- the matrix is 66049 rules; indented output is
    # ~5-9MB and only slows scheme load. One line, separators only.
    json.dump(scheme, f, indent=None, separators=(",", ":"))
print(f"wrote {out} ({len(rules)} rules)")