import os
import socket
import subprocess
import threading
import time
import sublime
import sublime_plugin

from User.launchers._pb_port import kill_existing, PORT

_si = subprocess.STARTUPINFO()
_si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
_si.wShowWindow = subprocess.SW_HIDE


def _wait_and_open(port, timeout=60):
    """Poll until Flask is accepting connections, then open the browser."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.25)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            os.startfile("http://127.0.0.1:%d" % port)
            return
        except OSError:
            s.close()
            time.sleep(0.5)


class PbFlaskSilentCommand(sublime_plugin.WindowCommand):
    """Start the PyBackup Flask app headlessly (no console window) and open its browser UI.

    Kills any prior process still holding port %d first, so a stale instance
    with cached engine state can't serve the freshly launched UI.

    The browser opens only after Flask is actually accepting connections —
    the ldsv.save_bp backup-scan thread can delay Flask's bind by 30+ seconds
    while it walks Google Drive remotes.

    Menu: Main.sublime-menu → Tools — "PyBackup Flask App (Silent)"
    Command palette: "PyBackup: Flask App (silent)"
    """ % PORT

    def run(self):
        _log = lambda msg: (sublime.status_message(f"[pb_flask_silent] {msg}"), print(f"[pb_flask_silent] {msg}"))
        _t0 = time.monotonic()
        _log(f"run() entered")

        def _bg():
            t0 = time.monotonic()
            kill_existing(PORT)
            _log(f"kill_existing done: {time.monotonic()-t0:.2f}s")
            from User.winutil._job import assign_pid
            proc = subprocess.Popen(
                [r"C:\Users\donal\AppData\Local\Programs\Python\Python312\python.exe", r"C:/Users/donal/projects/pybackup/ui/app.py"],
                creationflags=subprocess.CREATE_NO_WINDOW,
                startupinfo=_si,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            _log(f"Popen done: {time.monotonic()-t0:.2f}s, pid={proc.pid}")
            try:
                assign_pid(proc.pid)
            except Exception:
                pass
            _log(f"assign_pid done: {time.monotonic()-t0:.2f}s")
            # Open browser only after Flask is ready (background thread, non-blocking)
            threading.Thread(target=_wait_and_open, args=(PORT,), daemon=True).start()
            _log(f"wait thread started: {time.monotonic()-t0:.2f}s")

        threading.Thread(target=_bg, daemon=True).start()
        _log(f"bg thread launched: {time.monotonic()-_t0:.2f}s")