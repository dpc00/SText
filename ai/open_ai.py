"""Commands to launch Ai CLI from Sublime Text.

Command names as Sublime Text sees them:
  open_ai_here
  open_ai_in_editor
  open_ai_terminus_in_editor
  open_ai_terminus_here
"""

import glob
import json
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
            ["cmd", "/k", "ollama", "launch", "claude"],
            cwd=path,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    else:
        subprocess.Popen(["bash", "-i", "-c", "ollama", "launch", "claude"], cwd=path)


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
                "shell_cmd": "ollama launch claude",
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
                "shell_cmd": "ollama launch claude",
                "cwd": path,
                "title": "Ai",
            },
        )
        sublime.set_timeout(lambda: _mark_active_ai_view(self.window), 1000)

    def is_visible(self, paths=None):
        return True


def _get_response_tab(window):
    """Find or create the Claude Response scratch tab."""
    for v in window.views():
        if v.name() == "AI Response":
            return v
    v = window.new_file()
    v.set_name("AI Response")
    v.set_scratch(True)
    return v


def _last_claude_response():
    """Return the last assistant text from the most recent JSONL transcript."""
    pattern = os.path.expanduser("~/.claude/projects/**/*.jsonl")
    files = glob.glob(pattern)
    if not files:
        return None
    latest = max(files, key=os.path.getmtime)
    last_text = None
    try:
        with open(latest, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Records with role=assistant and text content
                msg = rec.get("message", {})
                if msg.get("role") == "assistant":
                    for block in msg.get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            last_text = block["text"]
    except OSError:
        return None
    return last_text


class ClaudeGrabResponseCommand(sublime_plugin.WindowCommand):
    """Grab Claude's last response from the transcript and open it in the Claude Response tab."""

    def run(self):
        text = _last_claude_response()
        if not text:
            sublime.status_message("No Claude response found in transcript")
            return
        v = _get_response_tab(self.window)
        v.set_read_only(False)
        separator = "\n\n--- CLAUDE ---\n"
        v.run_command(
            "append",
            {
                "characters": separator
                + text
                + "\n\n--- YOUR TURN (use >> for your lines) ---\n"
            },
        )
        self.window.focus_view(v)
        v.run_command("move_to", {"to": "eof"})


class ClaudeSendTabCommand(sublime_plugin.WindowCommand):
    """Send the Claude Response tab contents back to the Ai terminal."""

    def run(self):
        ai_view = None
        for v in self.window.views():
            if v.settings().get(_AI_VIEW_SETTING):
                ai_view = v
                break
        if ai_view is None:
            sublime.status_message("No Ai terminal found")
            return
        self.window.focus_view(ai_view)
        ai_view.run_command("terminus_send_string", {"string": "read tab\n"})
