"""Display caret for Claude-style TUIs that park the hardware cursor on a status bar.

Why this exists
---------------
Claude Code (main-screen mode) often CUPs to the footer to repaint token/cost
lines and *leaves* the hardware cursor there. The edit buffer still lives on
the `>` prompt row. A faithful PTY→ST caret mapping then puts the ST caret on
the footer while the user is editing the prompt — left/right appear to do
nothing until Claude happens to CUP back to the prompt (feels like multiple
keystrokes / wrong position).

Terminus feels fine when the TUI keeps the hardware cursor on the input field.
When the TUI parks the cursor on the status bar, every host that draws a
caret from the PTY cursor needs a display mapping. We only remap when we
detect a `>` prompt row *and* the hardware cursor is below the input box.

Column rules
------------
- Input starts after the prompt marker: `>` plus an optional space/NBSP (2 cells).
  That is why "beginning of line is 2 chars left of the cursor" when fully left —
  those two cells are the non-editable prompt, not a desync bug.
- Editable columns are [input_start, content_end]. content_end is after the last
  non-blank on the prompt row.
- We remember the last editable column while the hardware cursor is on the
  prompt; when parked on the status bar we restore that column.
"""


def find_prompt_row(screen):
    """Last grid row whose first cell is `>`, or None."""
    found = None
    for y in range(screen.rows):
        if screen.grid[y] and screen.grid[y][0] == ">":
            found = y
    return found


def input_start_col(screen, prompt_y):
    """First editable column on the prompt row (after `>` and optional blank)."""
    row = screen.grid[prompt_y]
    if not row or row[0] != ">":
        return 0
    if len(row) > 1 and row[1] in (" ", "\u00a0"):
        return 2
    return 1


def content_end_col(screen, prompt_y):
    """Column after last non-blank on the prompt (insert point at end of text)."""
    row = screen.grid[prompt_y]
    end = input_start_col(screen, prompt_y)
    for i, ch in enumerate(row):
        if ch not in (" ", "\u00a0"):
            end = i + 1
    # Empty input: seat at start of field (after `>` / `>\xa0`).
    start = input_start_col(screen, prompt_y)
    return min(max(end, start), screen.cols - 1)


def _clamp_input_col(screen, prompt_y, col):
    start = input_start_col(screen, prompt_y)
    end = content_end_col(screen, prompt_y)
    return min(max(int(col), start), end)


def note_hardware_on_prompt(screen):
    """If hardware cursor is on the prompt, remember editable column."""
    py = find_prompt_row(screen)
    if py is None or screen.y != py:
        return
    # Only trust x inside the editable span (ignore EOL erase CUPs past text).
    start = input_start_col(screen, py)
    end = content_end_col(screen, py)
    if start <= screen.x <= end:
        screen.input_caret_x = screen.x
    elif screen.x > end:
        # Past text (padding / erase) — remember end of text, not EOL.
        screen.input_caret_x = end


def adjust_display_caret(screen, cy, cx):
    """Map PTY cursor to ST caret; pin to prompt when parked on status footer."""
    py = find_prompt_row(screen)
    if py is None:
        return cy, cx

    hist = 0 if screen.alt_screen else len(screen.history)
    note_hardware_on_prompt(screen)

    # On prompt or its border: use hardware column, clamped to editable span
    # when on the prompt row itself.
    if screen.y == py:
        col = _clamp_input_col(screen, py, screen.x)
        screen.input_caret_x = col
        return hist + py, col
    if screen.y == py + 1:
        # Border under the input box — leave hardware as-is (rare).
        return cy, cx

    # Below the input box (status footer): pin to prompt at remembered column.
    if screen.y > py + 1:
        col = getattr(screen, "input_caret_x", None)
        if col is None:
            col = content_end_col(screen, py)
        else:
            col = _clamp_input_col(screen, py, col)
        return hist + py, col

    return cy, cx


def nudge_input_caret(screen, delta):
    """Optimistic left/right for when hardware is parked on the status bar.

    Returns True if display should refresh immediately.
    """
    py = find_prompt_row(screen)
    if py is None:
        return False
    # Only nudge when we would pin (hardware not on the prompt).
    if screen.y <= py + 1:
        return False
    col = getattr(screen, "input_caret_x", None)
    if col is None:
        col = content_end_col(screen, py)
    screen.input_caret_x = _clamp_input_col(screen, py, col + delta)
    return True


def pad_row_for_caret(rows, cy, cx):
    """Ensure row cy is long enough for caret column cx (after rstrip)."""
    if cy < 0 or cy >= len(rows):
        return rows
    row = list(rows[cy])
    while len(row) < cx:
        row.append((" ", 0))
    rows = list(rows)
    rows[cy] = row
    return rows
