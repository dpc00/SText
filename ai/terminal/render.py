"""Build view text + colour region list from screen cells.

Pure Python — no Sublime imports.
"""
from .colors import REVERSE, pack_attr, scope_name_for

# Baked into the ST color scheme (see ai_terminal._HOST_CURSOR_SCOPE). Used when
# the host synthesizes a cursor so visibility does not depend on dynamic
# ai.fb.* registration / flush races.
HOST_CURSOR_SCOPE = "ai.terminal.host_cursor"


def cell_needs_host_cursor(rows, cy, cx):
    """True when the PTY cursor cell is not already SGR-reverse (app cursor)."""
    if rows is None or cy is None or cx is None:
        return False
    if cy < 0 or cx < 0 or cy >= len(rows):
        return False
    row = rows[cy]
    if cx >= len(row):
        # Past rstripped blanks — shell/Grok insertion point.
        return True
    _ch, attr = row[cx]
    return not bool(attr & REVERSE)


# Full block glyph (display-only, never sent to the PTY).
# Bright-white-on-black attr: ST often drops region *fill* and keeps only
# foreground. Reverse-default is black-on-white → black █ on black terminal
# when fill is dropped = invisible. White █ stays visible without fill.
_HOST_CURSOR_GLYPH = "\u2588"  # █
_HOST_CURSOR_ATTR = pack_attr(fg=16, bg=1)  # → ai.fb.16.1 white on near-black


def paint_host_cursor(rows, cy, cx):
    """Ensure a visible cursor cell when the app did not SGR-reverse it.

    Terminus shows cursor via selection + pyte cursor; it does not rewrite
    mid-line glyphs. We only synthesize a block on *blank* insertion cells
    (EOL). Mid-line: leave the cell alone so typing does not flash reverse/█
    against Grok's per-key glyph paints.

    Returns (rows, host_painted).
    """
    if rows is None or cy is None or cx is None:
        return rows, False
    if cy < 0 or cx < 0 or cy >= len(rows):
        return rows, False
    rows = list(rows)
    row = list(rows[cy])
    # Cursor often sits past rstripped trailing spaces; pad so the cell exists.
    while len(row) <= cx:
        row.append((" ", 0))
    ch, attr = row[cx]
    if attr & REVERSE:
        rows[cy] = row
        return rows, False
    if not ch or ch in (" ", "\u00a0"):
        # EOL / blank insertion point — white block only (no mid-line rewrite).
        row[cx] = (_HOST_CURSOR_GLYPH, _HOST_CURSOR_ATTR)
        rows[cy] = row
        return rows, True
    # Mid-line with real glyph: trust the app cell (Terminus-like). ST selection
    # still tracks caret_offset in the host adapter.
    rows[cy] = row
    return rows, False


def cursor_text_offset(rows, cy, cx):
    """Byte/char offset of (cy, cx) in build_text_and_regions output, or None."""
    if rows is None or cy is None or cx is None:
        return None
    if cy < 0 or cx < 0 or cy >= len(rows):
        return None
    if cx >= len(rows[cy]):
        return None
    off = 0
    for i in range(cy):
        off += len(rows[i]) + 1  # +1 newline
    return off + cx


def punch_host_cursor_region(regions, off, end=None):
    """Make HOST_CURSOR_SCOPE exclusive over [off, end).

    ST draws overlapping add_regions scopes with undefined z-order: an ai.fb.*
    run that covers the cursor cell often wins mid-line (styled prompt text),
    so the grey host block only shows at EOL where no colour run exists.
    Split/remove any region covering the cell, then append host_cursor alone.
    """
    if off is None:
        return regions
    end = off + 1 if end is None else end
    if end <= off:
        return regions
    out = []
    for item in regions or []:
        b, e, scope = item[0], item[1], item[2]
        if e <= off or b >= end:
            out.append([b, e, scope])
            continue
        if b < off:
            out.append([b, off, scope])
        if e > end:
            out.append([end, e, scope])
    out.append([off, end, HOST_CURSOR_SCOPE])
    return out


def build_text_and_regions(rows, scope_for=None):
    """Flatten structured rows into view text + [begin, end, scope] regions.

    scope_for: optional callable(attr)->scope; defaults to pure scope_name_for.
    Adjacent equal-scope cells are coalesced. NBSP normalized to space.
    """
    if scope_for is None:
        scope_for = scope_name_for
    parts = []
    regs = []
    offset = 0
    for cells in rows:
        run_scope = None
        run_start = -1
        for ch, attr in cells:
            parts.append(ch)
            scope = scope_for(attr) if attr else None
            if scope != run_scope:
                if run_scope is not None:
                    regs.append([run_start, offset, run_scope])
                run_scope = scope
                run_start = offset
            offset += 1
        if run_scope is not None:
            regs.append([run_start, offset, run_scope])
        parts.append("\n")
        offset += 1
    if parts and parts[-1] == "\n":
        parts.pop()
    text = "".join(parts).replace(" ", " ")
    return text, regs
