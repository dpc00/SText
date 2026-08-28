"""ai_toggle_line_numbers.py — show or hide line numbers on the focused view.

Command palette: "View: Toggle Line Numbers"
"""

import sublime
import sublime_plugin


def _toggle_bool_setting(view, key):
    # These settings default to True in ST; if unset (None), treat as True so
    # the first toggle turns them off, then on, etc.
    cur = view.settings().get(key)
    view.settings().set(key, not cur if cur is not None else False)


def _target_view(window):
    # window.active_view() never returns an output-panel view (e.g. an Ai
    # Terminal parked in panel mode via GhostShell's ai_terminal_toggle_panel),
    # even while that panel has real keyboard focus -- it falls back to
    # whatever tab was last focused in a group. Resolve the actual focused
    # panel view first, matching GhostShell's own ai_terminal.py commands.
    panel = window.active_panel()
    if panel and panel.startswith("output."):
        view = window.find_output_panel(panel[len("output."):])
        if view is not None:
            return view
    return window.active_view()


class AiToggleLineNumbersCommand(sublime_plugin.WindowCommand):
    """Show or hide line numbers on the focused view (tab or panel, e.g. an Ai terminal).

    Command palette: "View: Toggle Line Numbers"
    """
    def run(self):
        view = _target_view(self.window)
        if view is not None:
            _toggle_bool_setting(view, "line_numbers")
