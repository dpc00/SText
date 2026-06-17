"""Commands to launch Codex CLI from Sublime Text.

Command names as Sublime Text sees them:
  open_codex_here
  open_codex_in_editor
  open_codex_terminus_in_editor
  open_codex_terminus_here
"""

import os
import subprocess
import sys

import sublime
import sublime_plugin


_CODEX_VIEW_SETTING = "codex_logger"


def _mark_active_codex_view(window):
    """Mark the newly opened Terminus view so codex_tab_manager can find it."""
    if not window:
        return
    view = window.active_view()
    if not view:
        return
    view.set_name("Codex")
    view.settings().set(_CODEX_VIEW_SETTING, True)


def _external_console(path):
    """Spawn an external terminal window running codex in path."""
    if sys.platform == "win32":
        subprocess.Popen(
            ["cmd", "/k", "codex"],
            cwd=path,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    else:
        subprocess.Popen(["bash", "-i", "-c", "codex"], cwd=path)


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


class OpenCodexHereCommand(sublime_plugin.WindowCommand):
    """Sidebar: open a new external console running Codex in the chosen dir."""

    def run(self, paths=None):
        path = _resolve_here_path(self.window, paths or [])
        if path:
            _external_console(path)

    def is_visible(self, paths=None):
        return bool(paths)


class OpenCodexInEditorCommand(sublime_plugin.TextCommand):
    """Palette/keybinding: open a new external console running Codex."""

    def run(self, edit):
        path = _resolve_editor_path(self.view)
        if path:
            _external_console(path)


class OpenCodexTerminusInEditorCommand(sublime_plugin.TextCommand):
    """Palette/keybinding: open a Terminus tab running Codex."""

    def run(self, edit):
        path = _resolve_editor_path(self.view)
        if not path:
            return
        self.view.window().run_command(
            "terminus_open",
            {
                "cmd": ["codex"],
                "cwd": path,
                "title": "Codex",
            },
        )
        sublime.set_timeout(lambda: _mark_active_codex_view(self.view.window()), 1000)


class OpenCodexTerminusHereCommand(sublime_plugin.WindowCommand):
    """Sidebar: open a Terminus tab running Codex in the chosen dir."""

    def run(self, paths=None):
        path = _resolve_here_path(self.window, paths or [])
        if not path:
            return
        self.window.run_command(
            "terminus_open",
            {
                "cmd": ["codex"],
                "cwd": path,
                "title": "Codex",
            },
        )
        sublime.set_timeout(lambda: _mark_active_codex_view(self.window), 1000)

    def is_visible(self, paths=None):
        return True
