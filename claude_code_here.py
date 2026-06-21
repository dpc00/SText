import os

import sublime_plugin


class ClaudeCodeHereCommand(sublime_plugin.WindowCommand):
    """Launch Claude Code with cwd pinned to the clicked location.

    Models the "Open Terminus here..." items. Resolves "here" from:
      1. the side-bar selection (`paths`, dir as-is / file -> its dir), else
      2. the active view's file directory, else
      3. the window's first project folder, else the home dir.
    Then defers to the plugin's own `claude_code_terminal` (model picker and
    all), so this stays a thin shim over the real launcher.
    """

    def run(self, paths=None):
        self.window.run_command(
            "claude_code_terminal", {"cwd": self._resolve_cwd(paths)})

    def _resolve_cwd(self, paths):
        if paths:
            p = paths[0]
            return p if os.path.isdir(p) else os.path.dirname(p)
        view = self.window.active_view()
        if view and view.file_name():
            return os.path.dirname(view.file_name())
        folders = self.window.folders()
        return folders[0] if folders else os.path.expanduser("~")
