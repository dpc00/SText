"""Launch pybackup's Textual TUI in Terminus (not ai_terminal).

ai_terminal is built for agent CLIs (key forwarding, force_main_screen, copy-first
mouse). Fullscreen Textual apps need a real terminal host — same reason Flask
uses Terminus rather than embedding the browser in a ConPTY tab.
"""

import sublime
import sublime_plugin

_PYTHON = r"C:\Users\donal\AppData\Local\Programs\Python\Python312\python.exe"
_TUI = r"C:\Users\donal\projects\pybackup\ui\tui.py"
_CWD = r"C:\Users\donal\projects\pybackup"


class PbTuiLauncherCommand(sublime_plugin.WindowCommand):
    """Open a Terminus tab running the pybackup Textual TUI.

    Menu: Tools → PyBackup Textual TUI
    Command palette: PyBackup: Textual TUI
    """

    def run(self):
        # Prefer argv form (no shell quoting). Fall back to shell_cmd if an
        # older Terminus only accepts that shape.
        args = {
            "title": "Pybackup TUI",
            "tag": "pb_tui",
            "cwd": _CWD,
            "cmd": [_PYTHON, _TUI],
        }
        try:
            self.window.run_command("terminus_open", args)
        except Exception:
            args.pop("cmd", None)
            args["shell_cmd"] = f'"{_PYTHON}" "{_TUI}"'
            self.window.run_command("terminus_open", args)
