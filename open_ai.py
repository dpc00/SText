"""Commands to launch Ai CLI from Sublime Text.

Command names as Sublime Text sees them:
  open_ai_here
  open_ai_in_editor
  open_ai_terminus_in_editor
  open_ai_terminus_here
"""

import os
import subprocess
import sys

import sublime
import sublime_plugin


_AI_VIEW_SETTING = "ai_logger"


def _mark_active_ai_view(window):
    """Mark the newly opened Terminus view so ai_tab_manager can find it."""
    if not window:
        return
    view = window.active_view()
    if not view:
        return
    view.set_name("Ai")
    view.settings().set(_AI_VIEW_SETTING, True)


def _external_console(path):
    """Spawn an external terminal window running ai in path."""
    if sys.platform == "win32":
        subprocess.Popen(
            ["cmd", "/k", "claude", "--chrome"],
            cwd=path,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    else:
        subprocess.Popen(["bash", "-i", "-c", "claude"], cwd=path)


def _resolve_editor_path(view):
    """Return the directory to use for an editor-triggered command."""
    path = view.file_name()
    if path:
        return os.path.dirname(path)
    window = view.window()
    folders = window.folders() if window else []
    return folders[0] if folders else None


def _resolve_here_path(window, paths):
    """Return the directory to use for a sidebar-triggered command."""
    if paths:
        path = paths[0]
        return path if os.path.isdir(path) else os.path.dirname(path)
    folders = window.folders()
    return folders[0] if folders else None


class OpenAiHereCommand(sublime_plugin.WindowCommand):
    """Sidebar: open a new external console running Ai in the chosen dir."""

    def run(self, paths=None):
        path = _resolve_here_path(self.window, paths or [])
        if path:
            _external_console(path)

    def is_visible(self, paths=None):
        return bool(paths)


class OpenAiInEditorCommand(sublime_plugin.TextCommand):
    """Palette/keybinding: open a new external console running Ai."""

    def run(self, edit):
        path = _resolve_editor_path(self.view)
        if path:
            _external_console(path)


class OpenAiTerminusInEditorCommand(sublime_plugin.TextCommand):
    """Palette/keybinding: open a Terminus tab running Ai."""

    def run(self, edit):
        path = _resolve_editor_path(self.view)
        if not path:
            return
        self.view.window().run_command(
            "terminus_open",
            {
                "cmd": ["claude", "--chrome"],
                "cwd": path,
                "title": "Ai",
            },
        )
        sublime.set_timeout(lambda: _mark_active_ai_view(self.view.window()), 1000)


class OpenAiTerminusHereCommand(sublime_plugin.WindowCommand):
    """Sidebar: open a Terminus tab running Ai in the chosen dir."""

    def run(self, paths=None):
        path = _resolve_here_path(self.window, paths or [])
        if not path:
            return
        self.window.run_command(
            "terminus_open",
            {
                "cmd": ["claude", "--chrome"],
                "cwd": path,
                "title": "Ai",
            },
        )
        sublime.set_timeout(lambda: _mark_active_ai_view(self.window), 1000)

    def is_visible(self, paths=None):
        return True
