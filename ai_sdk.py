"""ai_sdk.py — Claude via Agent SDK, no terminal required.

AiSdkQueryCommand  Ctrl+Alt+A → input panel → Claude answers in "Ai (SDK)" view
"""

import subprocess
import sys
import threading

import sublime  # type: ignore
import sublime_plugin  # type: ignore

_VIEW_NAME = "Ai (SDK)"
_DIVIDER = "─" * 60
_PYTHON = r"C:\Users\donal\AppData\Local\Programs\Python\Python312\python.exe"
_SCRIPT = r"C:\Users\donal\projects\tools\agent_query.py"


def _sdk_view(window):
    for v in window.views():
        if v.name() == _VIEW_NAME:
            return v
    v = window.new_file()
    v.set_name(_VIEW_NAME)
    v.set_scratch(True)
    v.settings().set("word_wrap", True)
    v.settings().set("gutter", False)
    v.settings().set("line_numbers", False)
    v.settings().set("ai_sdk_view", True)
    return v


def _append(view, text):
    def _do():
        view.set_read_only(False)
        view.run_command("append", {"characters": text, "scroll_to_end": True})
        view.set_read_only(True)
    sublime.set_timeout(_do, 0)


def _run(prompt, view):
    try:
        proc = subprocess.Popen(
            [_PYTHON, _SCRIPT, prompt],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )
        for chunk in iter(lambda: proc.stdout.read(256), ""):
            _append(view, chunk)
        proc.wait()
        if proc.returncode != 0:
            err = proc.stderr.read()
            if err:
                _append(view, f"\n[Error: {err.strip()}]\n")
    except Exception as e:
        _append(view, f"\n[Error: {e}]\n")
    finally:
        _append(view, "\n")


class AiSdkQueryCommand(sublime_plugin.WindowCommand):
    """Ctrl+Alt+A — ask Claude; response streams into the Ai (SDK) view."""

    def run(self):
        self.window.show_input_panel(
            "Ask Claude:", "", self._submit, None, None
        )

    def _submit(self, prompt):
        prompt = prompt.strip()
        if not prompt:
            return
        view = _sdk_view(self.window)
        _append(view, f"\n{_DIVIDER}\nYou: {prompt}\n{_DIVIDER}\n↺ thinking…\n")
        threading.Thread(target=_run, args=(prompt, view), daemon=True).start()
