"""Xterm mouse protocol encoding (host → PTY).

Apps enable tracking with DECSET:
  CSI ? 1000 h  — click only
  CSI ? 1002 h  — click + drag
  CSI ? 1003 h  — any-event (motion)
  CSI ? 1006 h  — SGR extended coordinates (preferred)

Reports (when 1006 / SGR is on):
  CSI < Cb ; Cx ; Cy M   press / wheel / motion
  CSI < Cb ; Cx ; Cy m   release

Cb: 0=left, 1=middle, 2=right, 64=wheel-up, 65=wheel-down;
    +32 motion, +4 shift, +8 meta, +16 ctrl.
Cx/Cy are 1-based cell columns/rows within the PTY grid.

Without 1006, legacy X10 (CSI M Cb+32 Cx+32 Cy+32) is used (coords ≤ 223).
"""

# Button codes (base, before modifier bits)
BTN_LEFT = 0
BTN_MIDDLE = 1
BTN_RIGHT = 2
BTN_RELEASE_X10 = 3  # X10-only "all buttons released"
BTN_WHEEL_UP = 64
BTN_WHEEL_DOWN = 65
BTN_WHEEL_LEFT = 66
BTN_WHEEL_RIGHT = 67

# ST button numbers (from drag_select event["button"]) → protocol base
_ST_BUTTON_TO_PROTO = {
    1: BTN_LEFT,
    2: BTN_MIDDLE,
    3: BTN_RIGHT,
}


def st_button_to_proto(st_button):
    """Map Sublime event button (1/2/3) to xterm button code, or None."""
    return _ST_BUTTON_TO_PROTO.get(int(st_button))


def encode_mouse(
    button,
    col,
    row,
    *,
    press=True,
    motion=False,
    sgr=True,
    shift=False,
    meta=False,
    ctrl=False,
):
    """Encode one mouse report for the PTY.

    button: protocol base (0/1/2/64/65/…) — not ST's 1-based button id.
    col/row: 1-based cell coordinates within the PTY screen.
    press: True → final M (SGR); False → final m (SGR release).
    motion: set +32 (button-event / any-event drag).
    """
    cb = int(button)
    if motion:
        cb |= 32
    if shift:
        cb |= 4
    if meta:
        cb |= 8
    if ctrl:
        cb |= 16
    col = max(1, int(col))
    row = max(1, int(row))
    if sgr:
        final = "M" if press else "m"
        return f"\x1b[<{cb};{col};{row}{final}"
    # Legacy X10: each component is a single byte = value + 32, max 223.
    col = min(col, 223)
    row = min(row, 223)
    if not press and button < 64:
        cb = BTN_RELEASE_X10
        if shift:
            cb |= 4
        if meta:
            cb |= 8
        if ctrl:
            cb |= 16
    cb = min(cb, 223)
    return "\x1b[M" + chr(cb + 32) + chr(col + 32) + chr(row + 32)


def encode_click(button, col, row, *, sgr=True, shift=False, meta=False, ctrl=False):
    """Press + release for a simple click (1000-mode apps)."""
    press = encode_mouse(
        button, col, row, press=True, sgr=sgr, shift=shift, meta=meta, ctrl=ctrl
    )
    release = encode_mouse(
        button, col, row, press=False, sgr=sgr, shift=shift, meta=meta, ctrl=ctrl
    )
    return press + release


def encode_wheel(direction_up, col, row, *, sgr=True, shift=False, meta=False, ctrl=False):
    """Wheel event (press only; no release in xterm). direction_up True → 64."""
    btn = BTN_WHEEL_UP if direction_up else BTN_WHEEL_DOWN
    return encode_mouse(
        btn, col, row, press=True, sgr=sgr, shift=shift, meta=meta, ctrl=ctrl
    )


def view_point_to_cell(row, col, *, hist_len, screen_rows, screen_cols):
    """Map a view (row, col) to 1-based PTY cell (cx, cy), or None if off-grid.

    hist_len: scrollback lines prepended in the view (0 on alt screen).
    row/col: 0-based Sublime rowcol of the click point.
    """
    screen_row = int(row) - int(hist_len)
    if screen_row < 0 or screen_row >= screen_rows:
        return None
    screen_col = max(0, min(int(col), screen_cols - 1))
    return screen_col + 1, screen_row + 1
