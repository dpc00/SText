"""Pure terminal core (no Sublime dependency).

Import these modules for unit tests outside ST. The ST plugin facade is
ai.ai_terminal which wires these into ConPTY + view rendering.
"""
from .colors import pack_attr, quantize256, scope_name_for, rstrip_cells
from .keys import translate_key
from .parser import Parser
from .render import build_text_and_regions, paint_host_cursor
from .screen import Screen

__all__ = [
    "Screen",
    "Parser",
    "pack_attr",
    "quantize256",
    "scope_name_for",
    "rstrip_cells",
    "translate_key",
    "build_text_and_regions",
    "paint_host_cursor",
]
