"""Frictionless idea / pain capture for Sublime Text.

Commands (bound in the keymap):
  ctrl+alt+i  capture_idea     - prompt for one line, append to ideas_inbox.md
  ctrl+alt+o  open_idea_inbox  - open ideas_inbox.md to review / check off

The inbox is a single global markdown file in the home directory, so it is the
same no matter which project is open.  Capturing is silent: it never switches
views or interrupts what you are doing - you type one line and you are back.
"""

import datetime
import os

import sublime
import sublime_plugin

_INBOX = os.path.join(os.path.expanduser("~"), "ideas_inbox.md")


def _ensure_inbox():
    if not os.path.exists(_INBOX):
        with open(_INBOX, "w", encoding="utf-8") as f:
            f.write("# Idea Inbox\n\n")


def _append_item(text):
    text = text.strip()
    if not text:
        return
    _ensure_inbox()
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(_INBOX, "a", encoding="utf-8") as f:
        f.write("- [ ] [%s] %s\n" % (ts, text))


class CaptureIdeaCommand(sublime_plugin.WindowCommand):
    """Prompt for a one-line idea or pain and append it to the global idea inbox.

    Key binding: ctrl+alt+i
    Command palette: "Ai: Capture Idea"
    """

    def run(self):
        self.window.show_input_panel(
            "Idea / pain:", "", self._on_done, None, None)

    def _on_done(self, text):
        if text.strip():
            _append_item(text)
            sublime.status_message("Captured to ideas_inbox.md")


class OpenIdeaInboxCommand(sublime_plugin.WindowCommand):
    """Open the global idea inbox markdown file for review and check-off.

    Key binding: ctrl+alt+o
    Command palette: "Ai: Open Idea Inbox"
    """

    def run(self):
        _ensure_inbox()
        self.window.open_file(_INBOX)
