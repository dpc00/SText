"""Pure terminal core (no Sublime dependency).

Import these modules for unit tests outside ST. The ST plugin facade is
ai.ai_terminal which wires these into ConPTY + view rendering.
"""
from .colors import pack_attr, quantize256, scope_name_for, rstrip_cells
from .keys import translate_key, encode_win32_key
from .mouse import (
    encode_click,
    encode_mouse,
    encode_wheel,
    st_button_to_proto,
    view_point_to_cell,
)
from .parser import Parser
from .pty_env import sanitize_pty_env
from .render import (
    HOST_CURSOR_SCOPE,
    build_text_and_regions,
    cell_needs_host_cursor,
    cursor_text_offset,
    paint_host_cursor,
    punch_host_cursor_region,
)
from .screen import Screen
from .caret import adjust_display_caret, pad_row_for_caret

__all__ = [
    "Screen",
    "Parser",
    "pack_attr",
    "quantize256",
    "scope_name_for",
    "rstrip_cells",
    "translate_key",
    "encode_win32_key",
    "build_text_and_regions",
    "paint_host_cursor",
    "cell_needs_host_cursor",
    "cursor_text_offset",
    "punch_host_cursor_region",
    "HOST_CURSOR_SCOPE",
    "sanitize_pty_env",
    "encode_mouse",
    "encode_click",
    "encode_wheel",
    "st_button_to_proto",
    "view_point_to_cell",
    "adjust_display_caret",
    "pad_row_for_caret",
]
