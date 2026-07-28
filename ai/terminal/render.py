"""Build view text + colour region list from screen cells.

Pure Python — no Sublime imports.
"""
from .colors import REVERSE, scope_name_for


def paint_host_cursor(rows, cy, cx):
    """Ensure a reverse-video cell at the PTY cursor for host visibility.

    The ST host caret is forced invisible (scheme caret = background) so
    Claude/ratatui reverse-video cursors are not doubled. Apps that never
    SGR-reverse the cursor cell (Grok --minimal, plain shells) then show
    no insertion point until a keystroke. Paint a synthetic reverse cell
    only when the cursor cell is not already reverse.
    """
    if rows is None or cy is None or cx is None:
        return rows
    if cy < 0 or cx < 0 or cy >= len(rows):
        return rows
    rows = list(rows)
    row = list(rows[cy])
    # Cursor often sits past rstripped trailing spaces; pad so the cell exists.
    while len(row) <= cx:
        row.append((" ", 0))
    ch, attr = row[cx]
    if not (attr & REVERSE):
        row[cx] = (ch, attr | REVERSE)
    rows[cy] = row
    return rows


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
