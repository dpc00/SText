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
    """Column after the last non-blank cell on the prompt row (min 2 after `>`)."""
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
    """
    prompt_y = find_prompt_row(screen)
    if prompt_y is None:
        return cy, cx
    # Input box is: prompt row, then a border row, then status. Anything below
    # the border is "parked on status" — snap back to the prompt.
    if screen.y <= prompt_y + 1:
        return cy, cx
    hist = 0 if screen.alt_screen else len(screen.history)
    return hist + prompt_y, prompt_caret_col(screen, prompt_y)
