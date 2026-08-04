"""ctypes bindings for libghostty-vt (subset used by ghostty_engine.py).

Pure Python -- no Sublime imports. Safe to unit-test outside ST.

Only wraps the C API surface GhostyParser actually needs: terminal
lifecycle, VT feed, mode/data queries, the render-state row/cell
iterators for the active grid, and grid_ref for scrollback rows (which
sit outside the render-state's viewport-only scope). See
include/ghostty/vt/*.h in the ghostty checkout for the full API.

DLL location is not yet packaged into this repo -- this is still the
validation phase (confirming the swap works before it goes anywhere near
ai_terminal.py's live path). Override via GHOSTTY_VT_DLL env var if the
build output moves.
"""
import ctypes
import os

DEFAULT_DLL_PATH = r"C:\Users\donal\tools\ghostty\zig-out\bin\ghostty-vt.dll"


def load_library(path=None):
    path = path or os.environ.get("GHOSTTY_VT_DLL", DEFAULT_DLL_PATH)
    return ctypes.CDLL(path)


# ---- types.h ----

GhosttyResult = ctypes.c_int
SUCCESS = 0
OUT_OF_MEMORY = -1
INVALID_VALUE = -2
OUT_OF_SPACE = -3
NO_VALUE = -4

GhosttyTerminal = ctypes.c_void_p
GhosttyRenderState = ctypes.c_void_p
GhosttyRenderStateRowIterator = ctypes.c_void_p
GhosttyRenderStateRowCells = ctypes.c_void_p


class GhosttyBuffer(ctypes.Structure):
    _fields_ = [
        ("ptr", ctypes.POINTER(ctypes.c_uint8)),
        ("cap", ctypes.c_size_t),
        ("len", ctypes.c_size_t),
    ]


# ---- color.h / style.h ----

class GhosttyColorRgb(ctypes.Structure):
    _fields_ = [("r", ctypes.c_uint8), ("g", ctypes.c_uint8), ("b", ctypes.c_uint8)]


STYLE_COLOR_NONE = 0
STYLE_COLOR_PALETTE = 1
STYLE_COLOR_RGB = 2


class _GhosttyStyleColorValue(ctypes.Union):
    _fields_ = [
        ("palette", ctypes.c_uint8),
        ("rgb", GhosttyColorRgb),
        ("_padding", ctypes.c_uint64),
    ]


class GhosttyStyleColor(ctypes.Structure):
    _fields_ = [("tag", ctypes.c_int), ("value", _GhosttyStyleColorValue)]


class GhosttyStyle(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("fg_color", GhosttyStyleColor),
        ("bg_color", GhosttyStyleColor),
        ("underline_color", GhosttyStyleColor),
        ("bold", ctypes.c_bool),
        ("italic", ctypes.c_bool),
        ("faint", ctypes.c_bool),
        ("blink", ctypes.c_bool),
        ("inverse", ctypes.c_bool),
        ("invisible", ctypes.c_bool),
        ("strikethrough", ctypes.c_bool),
        ("overline", ctypes.c_bool),
        ("underline", ctypes.c_int),
    ]

    def init(self):
        self.size = ctypes.sizeof(GhosttyStyle)
        return self


# ---- point.h ----

POINT_TAG_ACTIVE = 0
POINT_TAG_VIEWPORT = 1
POINT_TAG_SCREEN = 2
POINT_TAG_HISTORY = 3


class GhosttyPointCoordinate(ctypes.Structure):
    _fields_ = [("x", ctypes.c_uint16), ("y", ctypes.c_uint32)]


class _GhosttyPointValue(ctypes.Union):
    _fields_ = [
        ("coordinate", GhosttyPointCoordinate),
        ("_padding", ctypes.c_uint64 * 2),
    ]


class GhosttyPoint(ctypes.Structure):
    _fields_ = [("tag", ctypes.c_int), ("value", _GhosttyPointValue)]


def point(tag, x, y):
    p = GhosttyPoint()
    p.tag = tag
    p.value.coordinate.x = x
    p.value.coordinate.y = y
    return p


# ---- grid_ref.h ----

class GhosttyGridRef(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("node", ctypes.c_void_p),
        ("x", ctypes.c_uint16),
        ("y", ctypes.c_uint16),
    ]

    def init(self):
        self.size = ctypes.sizeof(GhosttyGridRef)
        return self


# ---- terminal.h ----

class GhosttyTerminalOptions(ctypes.Structure):
    _fields_ = [
        ("cols", ctypes.c_uint16),
        ("rows", ctypes.c_uint16),
        ("max_scrollback", ctypes.c_size_t),
    ]


TERMINAL_DATA_COLS = 1
TERMINAL_DATA_ROWS = 2
TERMINAL_DATA_CURSOR_X = 3
TERMINAL_DATA_CURSOR_Y = 4
TERMINAL_DATA_CURSOR_PENDING_WRAP = 5
TERMINAL_DATA_ACTIVE_SCREEN = 6
TERMINAL_DATA_CURSOR_VISIBLE = 7
TERMINAL_DATA_TOTAL_ROWS = 14
TERMINAL_DATA_SCROLLBACK_ROWS = 15
TERMINAL_DATA_MOUSE_TRACKING = 11
TERMINAL_DATA_COLOR_PALETTE = 21
TERMINAL_DATA_VIEWPORT_ACTIVE = 32

TERMINAL_OPT_COLOR_PALETTE = 14

SCREEN_PRIMARY = 0
SCREEN_ALTERNATE = 1

# DEC private modes (ansi=false -> packed value == raw mode number,
# see ghostty_mode_new() in modes.h: value | (ansi << 15)).
MODE_NORMAL_MOUSE = 1000
MODE_BUTTON_MOUSE = 1002
MODE_ANY_MOUSE = 1003
MODE_SGR_MOUSE = 1006
MODE_BRACKETED_PASTE = 2004
MODE_ALT_SCREEN_SAVE = 1049


# ---- render.h ----

RENDER_STATE_DATA_COLS = 1
RENDER_STATE_DATA_ROWS = 2
RENDER_STATE_DATA_DIRTY = 3
RENDER_STATE_DATA_ROW_ITERATOR = 4

RENDER_STATE_DIRTY_FALSE = 0
RENDER_STATE_DIRTY_PARTIAL = 1
RENDER_STATE_DIRTY_FULL = 2

RENDER_STATE_OPTION_DIRTY = 0

RENDER_STATE_ROW_DATA_DIRTY = 1
RENDER_STATE_ROW_DATA_CELLS = 3

RENDER_STATE_ROW_OPTION_DIRTY = 0

RENDER_STATE_ROW_CELLS_DATA_STYLE = 2
RENDER_STATE_ROW_CELLS_DATA_BG_COLOR = 5
RENDER_STATE_ROW_CELLS_DATA_FG_COLOR = 6
RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_UTF8 = 9


class Ghostty:
    """Thin function-signature layer over a loaded ghostty-vt CDLL."""

    def __init__(self, lib):
        self.lib = lib
        self._bind()

    def _bind(self):
        lib = self.lib

        def sig(name, argtypes, restype):
            fn = getattr(lib, name)
            fn.argtypes = argtypes
            fn.restype = restype
            return fn

        p = ctypes.c_void_p
        u16 = ctypes.c_uint16
        u32 = ctypes.c_uint32
        sz = ctypes.c_size_t
        i = ctypes.c_int

        self.terminal_new = sig(
            "ghostty_terminal_new",
            [p, ctypes.POINTER(GhosttyTerminal), GhosttyTerminalOptions],
            GhosttyResult,
        )
        self.terminal_free = sig("ghostty_terminal_free", [GhosttyTerminal], None)
        self.terminal_reset = sig("ghostty_terminal_reset", [GhosttyTerminal], None)
        self.terminal_resize = sig(
            "ghostty_terminal_resize", [GhosttyTerminal, u16, u16, u32, u32], GhosttyResult
        )
        self.terminal_vt_write = sig(
            "ghostty_terminal_vt_write", [GhosttyTerminal, ctypes.c_char_p, sz], None
        )
        self.terminal_mode_get = sig(
            "ghostty_terminal_mode_get",
            [GhosttyTerminal, u16, ctypes.POINTER(ctypes.c_bool)],
            GhosttyResult,
        )
        self.terminal_get = sig(
            "ghostty_terminal_get", [GhosttyTerminal, i, p], GhosttyResult
        )
        self.terminal_set = sig(
            "ghostty_terminal_set", [GhosttyTerminal, i, p], GhosttyResult
        )
        self.terminal_grid_ref = sig(
            "ghostty_terminal_grid_ref",
            [GhosttyTerminal, GhosttyPoint, ctypes.POINTER(GhosttyGridRef)],
            GhosttyResult,
        )

        self.grid_ref_style = sig(
            "ghostty_grid_ref_style",
            [ctypes.POINTER(GhosttyGridRef), ctypes.POINTER(GhosttyStyle)],
            GhosttyResult,
        )
        self.grid_ref_graphemes = sig(
            "ghostty_grid_ref_graphemes",
            [ctypes.POINTER(GhosttyGridRef), ctypes.POINTER(ctypes.c_uint32), sz, ctypes.POINTER(sz)],
            GhosttyResult,
        )

        self.render_state_new = sig(
            "ghostty_render_state_new", [p, ctypes.POINTER(GhosttyRenderState)], GhosttyResult
        )
        self.render_state_free = sig("ghostty_render_state_free", [GhosttyRenderState], None)
        self.render_state_update = sig(
            "ghostty_render_state_update", [GhosttyRenderState, GhosttyTerminal], GhosttyResult
        )
        self.render_state_get = sig(
            "ghostty_render_state_get", [GhosttyRenderState, i, p], GhosttyResult
        )
        self.render_state_set = sig(
            "ghostty_render_state_set", [GhosttyRenderState, i, p], GhosttyResult
        )
        self.render_state_row_iterator_new = sig(
            "ghostty_render_state_row_iterator_new",
            [p, ctypes.POINTER(GhosttyRenderStateRowIterator)],
            GhosttyResult,
        )
        self.render_state_row_iterator_free = sig(
            "ghostty_render_state_row_iterator_free", [GhosttyRenderStateRowIterator], None
        )
        self.render_state_row_iterator_next = sig(
            "ghostty_render_state_row_iterator_next",
            [GhosttyRenderStateRowIterator],
            ctypes.c_bool,
        )
        self.render_state_row_get = sig(
            "ghostty_render_state_row_get", [GhosttyRenderStateRowIterator, i, p], GhosttyResult
        )
        self.render_state_row_set = sig(
            "ghostty_render_state_row_set", [GhosttyRenderStateRowIterator, i, p], GhosttyResult
        )
        self.render_state_row_cells_new = sig(
            "ghostty_render_state_row_cells_new",
            [p, ctypes.POINTER(GhosttyRenderStateRowCells)],
            GhosttyResult,
        )
        self.render_state_row_cells_free = sig(
            "ghostty_render_state_row_cells_free", [GhosttyRenderStateRowCells], None
        )
        self.render_state_row_cells_next = sig(
            "ghostty_render_state_row_cells_next", [GhosttyRenderStateRowCells], ctypes.c_bool
        )
        self.render_state_row_cells_get = sig(
            "ghostty_render_state_row_cells_get",
            [GhosttyRenderStateRowCells, i, p],
            GhosttyResult,
        )
