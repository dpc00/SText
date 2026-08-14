import sublime
import sublime_plugin


# ST ships "View: Toggle Minimap / Side Bar / Tabs / ..." in the Command
# Palette but has NO toggle for gutter, line numbers, or fold buttons --
# those are view settings, not commands -- so there was no way for the user to
# flip them from the palette. These three TextCommands fill that gap. They
# act on the active view (terminal or code alike), so e.g. toggling line
# numbers off in the Ai terminal is a one-keystroke palette action, matching
# the built-in "View: Toggle *" pattern. See ai/view_toggles.sublime-commands
# for the palette entries.


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


class AiToggleGutterCommand(sublime_plugin.WindowCommand):
    """Show or hide the gutter on the focused view (tab or panel, e.g. an Ai terminal).

    Command palette (ai/view_toggles.sublime-commands): "View: Toggle Gutter"
    """
    def run(self):
        view = _target_view(self.window)
        if view is not None:
            _toggle_bool_setting(view, "gutter")


class AiToggleLineNumbersCommand(sublime_plugin.WindowCommand):
    """Show or hide line numbers on the focused view (tab or panel, e.g. an Ai terminal).

    Command palette (ai/view_toggles.sublime-commands): "View: Toggle Line Numbers"
    """
    def run(self):
        view = _target_view(self.window)
        if view is not None:
            _toggle_bool_setting(view, "line_numbers")


class AiToggleFoldButtonsCommand(sublime_plugin.WindowCommand):
    """Show or hide fold buttons on the focused view (tab or panel, e.g. an Ai terminal).

    Command palette (ai/view_toggles.sublime-commands): "View: Toggle Fold Buttons"
    """
    def run(self):
        view = _target_view(self.window)
        if view is not None:
            _toggle_bool_setting(view, "fold_buttons")