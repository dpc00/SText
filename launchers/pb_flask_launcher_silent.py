import os
import subprocess
import sublime_plugin

from User.launchers._pb_port import kill_existing, PORT

_si = subprocess.STARTUPINFO()
_si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
_si.wShowWindow = subprocess.SW_HIDE


class PbFlaskSilentCommand(sublime_plugin.WindowCommand):
    """Start the PyBackup Flask app headlessly (no console window) and open its browser UI.

    Kills any prior process still holding port %d first, so a stale instance
    with cached engine state can't serve the freshly launched UI.

    Menu: Main.sublime-menu → Tools — "PyBackup Flask App (Silent)"
    Command palette: "PyBackup: Flask App (silent)"
    """ % PORT

    def run(self):
        kill_existing(PORT)
        subprocess.Popen(
            ["python", r"C:/Users/donal/projects/pybackup/ui/app.py"],
            creationflags=subprocess.CREATE_NO_WINDOW,
            startupinfo=_si,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        os.startfile("http://127.0.0.1:%d" % PORT)