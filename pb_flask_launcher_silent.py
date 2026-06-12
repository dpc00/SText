import os
import subprocess
import sublime_plugin


class PbFlaskSilentCommand(sublime_plugin.WindowCommand):
    def run(self):
        subprocess.Popen(
            ["python", r"C:/Users/donal/projects/pybackup/ui/app.py"],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.startfile("http://127.0.0.1:5757")
