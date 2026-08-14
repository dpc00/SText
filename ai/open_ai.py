"""Commands for pulling Claude's last response into a Sublime tab.

Command names as Sublime Text sees them:
  claude_grab_response
"""

import glob
import json
import os

import sublime
import sublime_plugin


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
    patterns = [
        os.path.expanduser("~/.claude/projects/**/*.jsonl"),
        os.path.expanduser("~/.gemini/tmp/*/chats/*.jsonl"),
        os.path.expanduser("~/.gemini/tmp/*/chats/**/*.jsonl")
    ]
    files = []
    for pattern in patterns:
        files.extend(glob.glob(pattern, recursive=True))
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

                # Check for Claude CLI format
                msg = rec.get("message", {})
                if msg.get("role") == "assistant":
                    for block in msg.get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            last_text = block["text"]

                # Check for Gemini CLI format
                elif rec.get("type") == "gemini" and "content" in rec:
                    content = rec["content"]
                    if isinstance(content, str):
                        if content.strip():
                            last_text = content
                    elif isinstance(content, list):
                        parts = []
                        for part in content:
                            if isinstance(part, dict):
                                if "text" in part:
                                    parts.append(part["text"])
                            elif isinstance(part, str):
                                parts.append(part)
                        text = "".join(parts).strip()
                        if text:
                            last_text = text
    except OSError:
        return None
    return last_text


class ClaudeGrabResponseCommand(sublime_plugin.WindowCommand):
    """Grab Claude's last response from the JSONL transcript into the Claude Response tab.

    Key binding: ctrl+alt+g
    Command palette: "Claude: Grab Last Response"
    """

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
