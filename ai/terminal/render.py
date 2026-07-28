"""Build view text + colour region list from screen cells.

Pure Python — no Sublime imports.
"""
from .colors import scope_name_for


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
