"""Display caret for TUIs that park the hardware cursor off the input field.

Why this exists
---------------
Claude Code (main-screen mode) often CUPs to the footer to repaint token/cost
lines and *leaves* the hardware cursor there. The edit buffer still lives on
the `>` prompt row. A faithful PTY→ST caret mapping then puts the ST caret on
the footer while the user is editing the prompt — left/right appear to do
nothing until Claude happens to CUP back to the prompt (feels like multiple
keystrokes / wrong position).

Grok keeps the hardware cursor on its input row (`│ > … │` in a box). That row
must win over earlier transcript lines that also start with `>`. If we lock
onto a history `>` and remap, the ST caret jumps to the wrong line (e.g. mid-
scrollback) and the real command line looks cursorless.

Terminus feels fine when the TUI keeps the hardware cursor on the input field.
When the TUI parks the cursor on the status bar, every host that draws a
caret from the PTY cursor needs a display mapping. We only remap when we
detect a `>` prompt row *and* the hardware cursor is below the input box.

Column rules
------------
- Input starts after the prompt marker: `>` plus an optional space/NBSP.
  Leading spaces and box borders (`│`) are not the prompt marker.
- Editable columns are [input_start, content_end]. content_end is after the
  last non-blank of the *input field* (not right-side chrome / clock).
- We remember the last editable column while the hardware cursor is on the
  prompt; when parked on the status bar we restore that column.
"""

# Skip when scanning for `>`: spaces and Grok/box vertical borders.
# Without this, Grok's `│ >` row is invisible to find_prompt_row (first
# non-space is `│`), so an older transcript `> …` wins and the caret remaps
# to the wrong line.
_BOX_V = frozenset(
    (
        "\u2502",  # │
        "\u2503",  # ┃
        "\u2551",  # ║
        "\u2524",  # ┤
        "\u251c",  # ├
        "|",
    )
)
_PROMPT_PREFIX = frozenset((" ", "\u00a0")) | _BOX_V


def _prompt_marker_col(screen, prompt_y):
    """Column of the `>` prompt marker on the row, or None.

    Allows leading spaces and vertical box borders (Grok: `  │ > text │`).
    The marker must be the first non-prefix cell (not a `>` buried in text).
    """
    row = screen.grid[prompt_y]
    if not row:
        return None
    n = min(len(row), screen.cols)
    for i in range(n):
        ch = row[i]
        if ch in _PROMPT_PREFIX:
            continue
        return i if ch == ">" else None
    return None


def find_prompt_row(screen):
    """Last grid row with a `>` prompt marker (spaces/box border prefix OK).

    Prefers the bottom-most match so Grok's live input box wins over earlier
    transcript lines that also use a leading `>`.
    """
    found = None
    for y in range(screen.rows):
        if _prompt_marker_col(screen, y) is not None:
            found = y
    return found


def input_start_col(screen, prompt_y):
    """First editable column on the prompt row (after `>` and optional blank)."""
    row = screen.grid[prompt_y]
    if not row:
        return 0
    m = _prompt_marker_col(screen, prompt_y)
    if m is None:
        return 0
    # After `>`; skip one following space/NBSP if present.
    nxt = m + 1
    if nxt < len(row) and row[nxt] in (" ", "\u00a0"):
        return nxt + 1
    return nxt


# Long blank run after input text = pad before right-side chrome (Grok clock).
# Single/double spaces stay part of the typed field; 4+ ends the field.
_CONTENT_GAP = 4


def content_end_col(screen, prompt_y):
    """Column after last non-blank of the *input field* (not right-side chrome).

    Grok paints a clock on the same row as `>` (e.g. `7:52 AM` after a long
    pad). Using the absolute last non-blank parked the host caret on the clock
    so the command line looked cursorless. Stop at a run of GAP blanks; text
    after that gap is chrome. Box vertical borders end the field immediately.
    Empty field (only chrome) seats at input_start.
    """
    row = screen.grid[prompt_y]
    start = input_start_col(screen, prompt_y)
    end = start
    gap = 0
    seen_text = False
    n = min(len(row), screen.cols)
    for i in range(start, n):
        ch = row[i]
        if ch in _BOX_V:
            break
        if ch in (" ", "\u00a0"):
            gap += 1
            if seen_text and gap >= _CONTENT_GAP:
                break
            continue
        # Non-blank after a long pad from field start → right chrome only.
        if gap >= _CONTENT_GAP:
            break
        gap = 0
        seen_text = True
        end = i + 1
    return min(max(end, start), screen.cols - 1)


def field_right_limit(screen, prompt_y):
    """Max caret column inside the input field (not on right box border).

    Unlike content_end_col (end of typed text), this is the right edge of the
    editable area so mid-line cursors are not forced to EOL text.
    """
    row = screen.grid[prompt_y]
    start = input_start_col(screen, prompt_y)
    n = min(len(row), screen.cols)
    for i in range(start, n):
        if row[i] in _BOX_V:
            return max(start, i - 1)
    return max(start, n - 1)


def _clamp_input_col(screen, prompt_y, col):
    """Clamp to typed-text span (for footer-park restore only)."""
    start = input_start_col(screen, prompt_y)
    end = content_end_col(screen, prompt_y)
    return min(max(int(col), start), end)


def _clamp_live_col(screen, prompt_y, col):
    """Clamp hardware column on the live prompt row — trust PTY, not content_end.

    Clamping to content_end while the cursor is on the prompt fights mid-line
    edits and Grok's real x (caret jumps to EOL / lags a key behind).
    """
    start = input_start_col(screen, prompt_y)
    limit = field_right_limit(screen, prompt_y)
    return min(max(int(col), start), limit)


def clear_optimistic_caret(screen):
    """Drop pending optimistic column (PTY moved or non-insert key)."""
    for name in ("optimistic_x", "optimistic_base_x"):
        if hasattr(screen, name):
            try:
                delattr(screen, name)
            except Exception:
                setattr(screen, name, None)


def note_optimistic_insert(screen, delta=1):
    """Advance display caret before the PTY redraws (Space lag fix).

    Grok often leaves hardware x on the last glyph for a frame after Space;
    without this the host block sits on the word until the next key.
    Returns True if a display refresh should run immediately.
    """
    py = find_prompt_row(screen)
    if py is None:
        return False
    # Only while editing the prompt row (or about to pin to it).
    if screen.y != py and screen.y <= py + 1:
        return False
    base = int(screen.x) if screen.y == py else int(
        getattr(screen, "input_caret_x", None) or content_end_col(screen, py)
    )
    if screen.y == py:
        screen.optimistic_base_x = base
    else:
        screen.optimistic_base_x = base
    screen.optimistic_x = _clamp_live_col(screen, py, base + int(delta))
    screen.input_caret_x = screen.optimistic_x
    return True


def _display_col_on_prompt(screen, prompt_y):
    """Hardware column, with optimistic insert advance if PTY not caught up."""
    hw = int(screen.x)
    opt = getattr(screen, "optimistic_x", None)
    base = getattr(screen, "optimistic_base_x", None)
    if opt is not None and base is not None:
        if hw != base:
            # PTY cursor moved — trust it and clear optimism.
            clear_optimistic_caret(screen)
            col = hw
        else:
            # Still waiting on PTY; show the post-key column (e.g. after Space).
            col = int(opt)
    else:
        col = hw
    return _clamp_live_col(screen, prompt_y, col)


def note_hardware_on_prompt(screen):
    """If hardware cursor is on the prompt, remember editable column.

    Must remember the *live* column, including after a just-typed space.
    content_end_col ignores trailing spaces (clock-gap logic); collapsing
    input_caret_x to that made the caret sit on the last letter after Space
    until the next non-space key redraws.
    """
    py = find_prompt_row(screen)
    if py is None or screen.y != py:
        return
    col = _display_col_on_prompt(screen, py)
    screen.input_caret_x = col


def adjust_display_caret(screen, cy, cx):
    """Map PTY cursor to ST caret; pin to prompt when parked on status footer."""
    py = find_prompt_row(screen)
    if py is None:
        return cy, cx

    hist = 0 if screen.alt_screen else len(screen.history)
    note_hardware_on_prompt(screen)

    # Live on the prompt row: hardware column + optimistic Space/print advance.
    # Never collapse to content_end — trailing spaces are real insertion points.
    if screen.y == py:
        col = _display_col_on_prompt(screen, py)
        screen.input_caret_x = col
        return hist + py, col
    if screen.y == py + 1:
        # Border under the input box — leave hardware as-is (rare).
        return cy, cx

    # Below the input box (status footer): pin to prompt at remembered column.
    # Use live-field clamp (not content_end) so a remembered "after space"
    # position is not snapped back to the last non-blank.
    if screen.y > py + 1:
        col = getattr(screen, "input_caret_x", None)
        if col is None:
            col = content_end_col(screen, py)
        else:
            col = _clamp_live_col(screen, py, col)
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
    clear_optimistic_caret(screen)
    col = getattr(screen, "input_caret_x", None)
    if col is None:
        col = content_end_col(screen, py)
    screen.input_caret_x = _clamp_live_col(screen, py, col + delta)
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
