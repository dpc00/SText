"""Build view text + colour region list from screen cells.

Pure Python — no Sublime imports.
"""
from .colors import REVERSE, scope_name_for

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


def paint_host_cursor(rows, cy, cx):
    """Pad the PTY cursor cell; report whether the host must draw a block.

    ST host caret stays invisible so Claude reverse-video is not doubled.
    If the app already SGR-reversed the cell, host_painted=False (app cursor).
    Otherwise pad past rstrip so cursor_text_offset is valid and return
    host_painted=True — the ST host draws an HTML phantom block (not
    colour-scheme regions: one-cell fills proved unreliable).

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
    _ch, attr = row[cx]
    rows[cy] = row
    if attr & REVERSE:
        return rows, False
    return rows, True


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
