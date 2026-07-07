import os
import subprocess
import sublime
import sublime_plugin

_si = subprocess.STARTUPINFO()
_si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
_si.wShowWindow = subprocess.SW_HIDE


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
            creationflags=subprocess.CREATE_NO_WINDOW,
            startupinfo=_si,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        os.startfile("http://127.0.0.1:5199")
