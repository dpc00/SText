"""pyte-backed VT parser: drop-in replacement for Parser.feed().

parser.py hand-rolls a CSI/OSC state machine that (by its own comments)
drops DECSTBM scroll regions, ICH/DCH/IL/DL, and never tracks DECTCEM
cursor visibility. pyte is a maintained VT100/xterm-compatible engine
that handles all of that. This module feeds bytes through pyte's
Stream/HistoryScreen, then copies the resolved cell state into our own
Screen's grid/attrs/x/y/history each feed() -- so render.py, caret.py,
and mouse.py (which read Screen.grid / .private_modes / .mouse_tracking
directly) see the exact same surface Parser produced, unchanged.

Pure Python -- no Sublime imports. Safe to unit-test outside ST.
"""
import os
import re
import sys

# ST's plugin_host is a separate embedded interpreter from any system
# Python -- `pip install pyte` (used during development) does not put it on
# plugin_host's sys.path, and this repo has no Package Control dependency
# mechanism wired up. pyte (and its one dependency, wcwidth) are pure
# Python with no compiled parts, so they're vendored straight into the repo
# instead. Only added to sys.path if a real install isn't already found,
# so a dev machine's pip-installed pyte still wins there.
try:
    import pyte
except ImportError:
    _vendor_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "vendor")
    if _vendor_dir not in sys.path:
        sys.path.insert(0, _vendor_dir)
    import pyte

from .colors import pack_attr, quantize256, BOLD, REVERSE, FAINT
from .screen import BLANK

_SGR_RE = re.compile(r"\x1b\[([0-9:;]*)m")


def _flatten_colon_param(tok):
    """One colon-form SGR chunk (e.g. '38:2:0:255:128:64') -> flat int list.

    Mirrors parser.py's _parse_ext_color: colon form may carry a colour-space
    id before R/G/B (explicit '0' or an empty field); pyte's own semicolon
    SGR handler expects exactly `mode;2;R;G;B` or `mode;5;N` with no CS, so
    it is dropped here rather than passed through.
    """
    nums = [int(x) if x.isdigit() else None for x in tok.split(":")]
    if len(nums) < 2 or nums[0] is None:
        return [n for n in nums if n is not None]
    mode_sel, sub = nums[0], nums[1]
    if sub == 5:
        n = nums[2] if len(nums) > 2 and nums[2] is not None else 0
        return [mode_sel, 5, n]
    if sub == 2:
        vals = [v for v in nums[2:] if v is not None]
        if len(vals) >= 3:
            r, g, b = vals[-3], vals[-2], vals[-1]
            return [mode_sel, 2, r, g, b]
        return [mode_sel, 2]
    return [n for n in nums if n is not None]


def _normalize_sgr_params(match):
    out = []
    for tok in match.group(1).split(";"):
        if ":" in tok:
            out.extend(str(n) for n in _flatten_colon_param(tok))
        else:
            out.append(tok)
    return "\x1b[" + ";".join(out) + "m"


def normalize_sgr_colons(text):
    """Rewrite ISO-8613-6 colon-form SGR sequences to semicolon form.

    pyte's Stream does not parse `:`-delimited SGR subparameters at all
    (confirmed empirically: it drops back to ground state mid-sequence and
    prints stray digits) -- only the older semicolon form. Junie/other
    Compose TUIs emit colon form for truecolor, so this pre-pass runs ahead
    of pyte on every feed() to keep that codepath working.
    """
    if ":" not in text:
        return text
    return _SGR_RE.sub(_normalize_sgr_params, text)

# 1-based fg/bg ids used throughout this codebase; 0 = default (matches
# colors.py's convention, which is 1-based xterm-256 index + 1).
_ANSI_NAME_TO_ID = {
    "black": 1, "red": 2, "green": 3, "brown": 4, "blue": 5, "magenta": 6,
    "cyan": 7, "white": 8,
    "brightblack": 9, "brightred": 10, "brightgreen": 11, "brightbrown": 12,
    "brightblue": 13, "brightmagenta": 14, "brightcyan": 15, "brightwhite": 16,
}


def _color_id(value):
    """pyte Char.fg/.bg (name string or 6-hex-digit string) -> our 1-based id."""
    if not value or value == "default":
        return 0
    if value in _ANSI_NAME_TO_ID:
        return _ANSI_NAME_TO_ID[value]
    try:
        r = int(value[0:2], 16)
        g = int(value[2:4], 16)
        b = int(value[4:6], 16)
    except (ValueError, IndexError):
        return 0
    return quantize256(r, g, b) + 1


def _cell_attr(cell):
    flags = 0
    if cell.bold:
        flags |= BOLD
    if cell.reverse:
        flags |= REVERSE
    fg = _color_id(cell.fg)
    bg = _color_id(cell.bg)
    if not cell.data or cell.data == " ":
        # A space's foreground is never visible. Unlike the hand-rolled
        # parser (whose erase_line/erase_display always wrote attr 0), pyte
        # correctly fills erased cells with the *current* SGR -- real VT
        # behaviour -- so a coloured-foreground erase (`ESC[K` right after
        # a fg-only SGR) leaves invisible-but-nonzero attrs on trailing
        # blanks. Screen.render_cells()'s rstrip only trims exact (" ", 0)
        # cells, so those survive as full-width padding instead of being
        # trimmed. Dropping fg here (bg/reverse still matter -- they ARE
        # visible on a space) restores the old trim behaviour with no
        # visible difference. Confirmed via real-session replay diff.
        if not bg and not (flags & REVERSE):
            fg = 0
    return pack_attr(fg, bg, flags)


class _AdaptedScreen(pyte.HistoryScreen):
    """Forwards DEC private modes (1049 alt-screen, 1000-1006 mouse, 2004
    paste, ...) to our outer Screen's private_modes set, exactly like
    parser.py's _dispatch_csi did -- render.py/mouse.py/caret.py read
    Screen.private_modes / .mouse_tracking / .alt_screen directly and must
    keep seeing them. pyte itself does not implement alt-buffer swapping
    (confirmed empirically): 1049 here only toggles the outer .alt_screen
    flag that controls whether render_cells() includes scrollback, which
    matches this codebase's existing single-buffer alt-screen model.
    """

    def __init__(self, outer, *a, **kw):
        self._outer = outer
        super().__init__(*a, **kw)

    def set_mode(self, *modes, **kwargs):
        if kwargs.get("private"):
            for mode in modes:
                self._outer.set_private_mode(mode, True)
                if mode == 1049 and not self._outer_force_main_screen:
                    self._outer.alt_screen = True
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        if kwargs.get("private"):
            for mode in modes:
                self._outer.set_private_mode(mode, False)
                if mode == 1049:
                    self._outer.alt_screen = False
        super().reset_mode(*modes, **kwargs)

    def reset(self):
        super().reset()
        self._outer.reset()

    def report_device_status(self, mode, private=False):
        # vendored pyte's Stream (streams.py _parser_fsm) tacks
        # private=True onto *any* CSI handler reached via a `?`-prefixed
        # sequence, not just set_mode/reset_mode -- but pyte's own
        # report_device_status(self, mode) takes no such kwarg. ConPTY
        # sends a private DSR (`\x1b[?...n`) at spawn on every launch
        # (confirmed live: hit cmd.exe and Kimi both), which raised
        # TypeError inside feed(), killed the reader thread, and printed
        # "[process exited]" though the child was still alive. The `?`
        # only matters for DECXCPR (extended cursor position); pyte
        # doesn't distinguish that from plain DSR anyway, so it's safe to
        # just drop the flag and delegate.
        super().report_device_status(mode)

    # set on construction by PyteParser; kept off the outer Screen so this
    # adapter stays the only thing that knows about the flag's interaction
    # with 1049.
    _outer_force_main_screen = True


class PyteParser:
    """Same contract as Parser: __init__(screen, force_main_screen), feed(text)."""

    def __init__(self, screen, force_main_screen=True):
        self.s = screen
        self.force_main_screen = force_main_screen
        cap = screen.history.maxlen or 1
        self._pt = _AdaptedScreen(screen, screen.cols, screen.rows, history=max(1, cap))
        self._pt._outer_force_main_screen = force_main_screen
        self._stream = pyte.Stream(self._pt)

    def feed(self, text):
        self._stream.feed(normalize_sgr_colons(text))
        self._sync()

    def resize(self, cols, rows):
        self._pt.resize(lines=rows, columns=cols)
        self.s.resize(cols, rows)

    def reset(self):
        # AiTerminalNukeCommand calls screen.reset() directly, bypassing
        # feed() -- pyte's buffer must be cleared too, or the next _sync()
        # repaints the "nuked" content straight back from self._pt.
        # _AdaptedScreen.reset() cascades into self.s.reset() already.
        self._pt.reset()

    def _sync(self):
        pt = self._pt
        s = self.s
        for y in range(pt.lines):
            line = pt.buffer.get(y)
            grow = s.grid[y]
            arow = s.attrs[y]
            for x in range(pt.columns):
                cell = line[x] if line is not None else None
                if cell is not None and cell.data:
                    grow[x] = cell.data
                    arow[x] = _cell_attr(cell)
                else:
                    grow[x] = BLANK
                    arow[x] = 0
        s.x = min(pt.cursor.x, s.cols - 1)
        s.y = min(pt.cursor.y, s.rows - 1)
        top = pt.history.top
        if top:
            # APPEND, do not clear() -- Screen.history is the accumulated
            # scrollback across every feed() call. pyte's own history.top
            # only holds lines scrolled off since the last drain (below).
            # This used to clear() first, so any TUI drawing progressively
            # across multiple feed() calls (e.g. Qwen's multi-line startup
            # banner) had each earlier increment wiped out by the next --
            # only the most recent chunk's scrolled lines ever survived.
            # Confirmed via real-session replay: OLD parser kept all 6 rows
            # of Qwen's ASCII banner in scrollback, this kept only 1.
            for line in top:
                # Full-width scan, matching the grid loop above -- line is a
                # sparse dict, and .items() alone silently drops any column
                # a TUI reached via cursor-forward (CSI 1C) rather than by
                # writing a literal space, gluing words together with no
                # gap. Confirmed via real-session replay diff (Claude Code's
                # header row scrolling into history).
                cells = []
                for x in range(pt.columns):
                    cell = line[x]
                    cells.append((cell.data, _cell_attr(cell)) if cell.data else (" ", 0))
                s.history.append(_rstrip(cells))
            top.clear()
        s.dirty = True


def _rstrip(cells):
    end = len(cells)
    while end > 0 and cells[end - 1] == (" ", 0):
        end -= 1
    return cells[:end]
