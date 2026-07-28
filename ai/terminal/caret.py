"""Display-caret helpers for TUIs that park the hardware cursor on a status bar.

Claude Code (and similar) repeatedly CUP to the footer to repaint token/cost
lines and often leave the real cursor there between keystrokes. The input
prompt stays on a `>` row above. Sublime's caret should sit on the prompt so
typing feels right even when the PTY cursor is temporarily on the footer.

Pure Python — no Sublime imports.
"""


def find_prompt_row(screen):
    """Return the last grid row whose first cell is `>`, or None."""
    found = None
    for y in range(screen.rows):
        if screen.grid[y] and screen.grid[y][0] == ">":
            found = y
    return found


def prompt_caret_col(screen, prompt_y):
    """Best-effort column for the prompt when we never saw a hardware CUP there.

    Prefer the last hardware column recorded while the cursor was on the prompt
    (`screen.input_caret_x`). Fall back to after the last non-blank cell.
    """
    ix = getattr(screen, "input_caret_x", None)
    if ix is not None:
        return min(max(int(ix), 0), screen.cols - 1)
    row = screen.grid[prompt_y]
    end = 0
    for i, ch in enumerate(row):
        if ch not in (" ", "\u00a0"):
            end = i + 1
    # Empty prompt `> ` / `>\xa0` — seat caret just after the prompt marker.
    if end < 2 and row and row[0] == ">":
        end = 2
    return min(max(end, 0), screen.cols - 1)


def adjust_display_caret(screen, cy, cx):
    """Possibly rewrite (cy, cx) from render_cells for ST caret placement.

    cy/cx are already in rendered-row space (history prepended when not alt).
    When the hardware cursor sits below the input box (status footer), pin the
    display caret to the prompt row instead.

    Also records `screen.input_caret_x` whenever the hardware cursor is on the
    prompt so a later status-bar park restores the exact column (not end-1).
    """
    prompt_y = find_prompt_row(screen)
    if prompt_y is None:
        return cy, cx
    # Remember the real input column while Claude has the cursor on `>`.
    if screen.y == prompt_y:
        screen.input_caret_x = screen.x
        return cy, cx
    # Input box is: prompt row, then a border row, then status. Anything below
    # the border is "parked on status" — snap back to the prompt.
    if screen.y <= prompt_y + 1:
        return cy, cx
    hist = 0 if screen.alt_screen else len(screen.history)
    return hist + prompt_y, prompt_caret_col(screen, prompt_y)


def pad_row_for_caret(rows, cy, cx):
    """Ensure rendered row `cy` is long enough that ST can place caret at `cx`.

    When the hardware cursor is not on the prompt row, render_cells rstrips that
    row and drops the blank cell the terminal keeps under the cursor. Without
    padding, `line_start + cx` clamps to EOL and the caret sits one cell left
    of Claude's real input column (and block carets look 'one off').
    """
    if cy < 0 or cy >= len(rows):
        return rows
    row = list(rows[cy])
    # Need len(row) >= cx so caret column cx is a valid gap (0..len).
    while len(row) < cx:
        row.append((" ", 0))
    rows = list(rows)
    rows[cy] = row
    return rows
