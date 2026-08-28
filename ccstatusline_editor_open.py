"""ccstatusline_editor_open.py — Sublime command that launches the
ccstatusline-editor server.

Top-level .py, auto-loaded by ST like any package's plugin file (no loader
needed for a single command class). Formerly managed by STRepoInstall as a
personal attachment; that project is retired, this is now a plain flat
plugin file directly in this package.
"""

import subprocess
import webbrowser

import sublime  # type: ignore
import sublime_plugin  # type: ignore

PORT = 5199


def _hidden_popen_kwargs():
    if sublime.platform() != "windows":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": si,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
    }


class CcstatuslineEditorOpenCommand(sublime_plugin.WindowCommand):
    """Launch the ccstatusline-editor server and open its web UI in a browser.

    Key binding: ctrl+alt+l
    Command palette: "CC Statusline Editor"
    """

    def run(self):
        cmd = ["ccstatusline-editor"]
        config_path = sublime.load_settings("Preferences.sublime-settings").get("ccstatusline_config_path", "")
        if config_path:
            cmd += ["--config", config_path]
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **_hidden_popen_kwargs()
        )
        webbrowser.open("http://127.0.0.1:%d" % PORT)
