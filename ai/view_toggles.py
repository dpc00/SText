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


class AiToggleGutterCommand(sublime_plugin.TextCommand):
    """View: Toggle Gutter -- show/hide the gutter on the active view."""
    def run(self, edit):
        _toggle_bool_setting(self.view, "gutter")


class AiToggleLineNumbersCommand(sublime_plugin.TextCommand):
    """View: Toggle Line Numbers -- show/hide line numbers on the active view."""
    def run(self, edit):
        _toggle_bool_setting(self.view, "line_numbers")


class AiToggleFoldButtonsCommand(sublime_plugin.TextCommand):
    """View: Toggle Fold Buttons -- show/hide fold +/- markers on the active view."""
    def run(self, edit):
        _toggle_bool_setting(self.view, "fold_buttons")