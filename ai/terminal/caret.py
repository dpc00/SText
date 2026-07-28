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


def _last_nonblank_col(screen, prompt_y):
    """Index of last non-blank cell on the prompt row, or -1 if none."""
    row = screen.grid[prompt_y]
    last = -1
    for i, ch in enumerate(row):
        if ch not in (" ", "\u00a0"):
            last = i
    return last


def prompt_content_end(screen, prompt_y):
    """Column after the last non-blank on the prompt (min 2 after `>`)."""
    end = _last_nonblank_col(screen, prompt_y) + 1
    row = screen.grid[prompt_y]
    if end < 2 and row and row[0] == ">":
        end = 2
    return min(max(end, 0), screen.cols - 1)


def display_col_on_prompt(screen, prompt_y):
    """Map hardware cursor on the prompt row to an ST insert column.

    Rules:
      - mid-line (x < last glyph): use x
      - on last glyph (x == last): use content end (after last glyph)
        Claude/ConPTY often reports the cursor *on* the final character of the
        input while the insert point is after it — that was the persistent
        one-column-left off-by-one.
      - past last glyph (x > last): clamp to content end (ignore EOL erase CUPs)
    """
    last = _last_nonblank_col(screen, prompt_y)
    end = prompt_content_end(screen, prompt_y)
    x = screen.x
    if last < 0:
        return min(max(x, 2), end, screen.cols - 1)
    if x < last:
        return x
    # x == last (on final glyph) or x > last (blank/EOL) → after content
    return end


def prompt_caret_col(screen, prompt_y):
    """Best-effort ST caret column for the prompt row when parked on status."""
    end = prompt_content_end(screen, prompt_y)
    ix = getattr(screen, "input_caret_x", None)
    if ix is not None and 0 <= int(ix) <= end:
        return min(max(int(ix), 0), end)
    return end


def adjust_display_caret(screen, cy, cx):
    """Possibly rewrite (cy, cx) from render_cells for ST caret placement.

    cy/cx are already in rendered-row space (history prepended when not alt).
    When the hardware cursor sits below the input box (status footer), pin the
    display caret to the prompt row instead.
    """
    prompt_y = find_prompt_row(screen)
    if prompt_y is None:
        return cy, cx
    hist = 0 if screen.alt_screen else len(screen.history)
    # Remember / use the real input column while Claude has the cursor on `>`.
    if screen.y == prompt_y:
        disp = display_col_on_prompt(screen, prompt_y)
        screen.input_caret_x = disp
        return hist + prompt_y, disp
    # Input box is: prompt row, then a border row, then status. Anything below
    # the border is "parked on status" — snap back to the prompt.
    if screen.y <= prompt_y + 1:
        return cy, cx
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
