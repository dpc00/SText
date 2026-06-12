import sublime
import sublime_plugin
import subprocess

FLASK_URL = "http://127.0.0.1:5757"
FLASK_CMD = "python -u C:/Users/donal/projects/pybackup/ui/app.py\n"
TAG = "pb_flask"


class PbFlaskLauncherCommand(sublime_plugin.WindowCommand):
    def run(self):
        self.window.run_command("terminus_open", {
            "title": "Pybackup Flask App",
            "tag": TAG,
        })
        subprocess.Popen("start chrome", shell=True)
        sublime.set_timeout(self._wait_for_chrome, 500)

    def _wait_for_chrome(self):
        output = subprocess.check_output(
            'tasklist /fi "IMAGENAME eq chrome.exe"', shell=True
        ).decode()
        if "chrome.exe" in output:
            subprocess.Popen(f"start chrome {FLASK_URL}", shell=True)
            self.window.run_command("terminus_send_string", {
                "string": FLASK_CMD,
                "tag": TAG,
            })
        else:
            sublime.set_timeout(self._wait_for_chrome, 500)
