import subprocess
import sublime
import sublime_plugin

from User.winutil._platform import hidden_popen_kwargs, open_url


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
            **hidden_popen_kwargs()
        )
        open_url("http://127.0.0.1:5199")
