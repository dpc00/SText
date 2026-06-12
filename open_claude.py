"""open_claude.py — commands to launch Claude Code CLI from Sublime Text.

Six commands, two delivery mechanisms × three entry points:

  Delivery:
    • External console  — opens a new cmd/bash window (no Terminus required).
                          Good when you want Claude in a separate window.
    • Terminus panel    — opens Claude inside a Terminus tab within ST.
                          "WithChrome" variants pass --chrome to enable the
                          Claude-in-Chrome MCP browser tool.

  Entry points (how the working directory is determined):
    • "Here"   — sidebar context menu; uses the right-clicked folder/file's dir.
    • "Editor" — keyboard/palette; uses the active file's dir, or the first
                 project folder if the view has no file on disk.

Command names (snake_case as ST sees them):
  open_claude_here                    — external console, sidebar
  open_claude_in_editor               — external console, active view
  open_claude_terminus_in_editor      — Terminus panel, active view
  open_claude_terminus_here           — Terminus panel, sidebar
  open_claude_terminus_in_editor_with_chrome  — Terminus + --chrome, active view
  open_claude_terminus_here_with_chrome       — Terminus + --chrome, sidebar
"""

import os
import subprocess
import sys

import sublime_plugin


def _external_console(path):
    """Spawn an external terminal window running 'claude' in *path*."""
    if sys.platform == "win32":
        # CREATE_NEW_CONSOLE opens a new cmd.exe window; /k keeps it open after
        # claude exits so you can read any error output.
        subprocess.Popen(
            ["cmd", "/k", "claude"],
            cwd=path,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    else:
        subprocess.Popen(["bash", "-i", "-c", "claude"], cwd=path)


def _resolve_editor_path(view):
    """Return the directory to use for an editor-triggered command.

    Preference order:
      1. Directory of the active file (if the view has a file on disk).
      2. First project folder (fallback for scratch/unsaved views).
      3. None if neither is available.
    """
    path = view.file_name()
    if path:
        return os.path.dirname(path)
    window = view.window()
    folders = window.folders() if window else []
    return folders[0] if folders else None


def _resolve_here_path(window, paths):
    """Return the directory to use for a sidebar-triggered command.

    *paths* is the list ST passes from the sidebar right-click.  If the
    clicked item is a file, use its parent directory.  Fall back to the
    first project folder when *paths* is empty (command palette invocation).
    """
    if paths:
        path = paths[0]
        return path if os.path.isdir(path) else os.path.dirname(path)
    folders = window.folders()
    return folders[0] if folders else None


# ── external-console commands ─────────────────────────────────────────────────


class OpenClaudeHereCommand(sublime_plugin.WindowCommand):
    """Sidebar: open a new external console running Claude in the chosen dir."""

    def run(self, paths=None):
        path = _resolve_here_path(self.window, paths or [])
        if path:
            _external_console(path)

    def is_visible(self, paths=None):
        return bool(paths)


class OpenClaudeInEditorCommand(sublime_plugin.TextCommand):
    """Palette/keybinding: open a new external console running Claude in the active file's dir."""

    def run(self, edit):
        path = _resolve_editor_path(self.view)
        if path:
            _external_console(path)


# ── Terminus-panel commands ───────────────────────────────────────────────────


class OpenClaudeTerminusInEditorCommand(sublime_plugin.TextCommand):
    """Palette/keybinding: open a Terminus tab running Claude in the active file's dir."""

    def run(self, edit):
        path = _resolve_editor_path(self.view)
        if not path:
            return
        self.view.window().run_command(
            "terminus_open",
            {
                "cmd": ["claude"],
                "cwd": path,
                "title": "Claude",
            },
        )


class OpenClaudeTerminusHereCommand(sublime_plugin.WindowCommand):
    """Sidebar: open a Terminus tab running Claude in the chosen dir."""

    def run(self, paths=None):
        path = _resolve_here_path(self.window, paths or [])
        if not path:
            return
        self.window.run_command(
            "terminus_open",
            {
                "cmd": ["claude"],
                "cwd": path,
                "title": "Claude",
            },
        )

    def is_visible(self, paths=None):
        return True


# ── Terminus + --chrome variants ──────────────────────────────────────────────
# '--chrome' enables the Claude-in-Chrome MCP tool, letting Claude control
# the browser via the Chrome extension.


class OpenClaudeTerminusInEditorWithChromeCommand(sublime_plugin.TextCommand):
    """Palette/keybinding: open a Terminus tab running 'claude --chrome' in the active file's dir."""

    def run(self, edit):
        path = _resolve_editor_path(self.view)
        if not path:
            return
        self.view.window().run_command(
            "terminus_open",
            {
                "cmd": ["claude", "--chrome"],
                "cwd": path,
                "title": "Claude (Chrome)",
            },
        )


class OpenClaudeTerminusHereWithChromeCommand(sublime_plugin.WindowCommand):
    """Sidebar: open a Terminus tab running 'claude --chrome' in the chosen dir."""

    def run(self, paths=None):
        path = _resolve_here_path(self.window, paths or [])
        if not path:
            return
        self.window.run_command(
            "terminus_open",
            {
                "cmd": ["claude", "--chrome"],
                "cwd": path,
                "title": "Claude (Chrome)",
            },
        )

    def is_visible(self, paths=None):
        return True
