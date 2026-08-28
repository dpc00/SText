"""pb_flask_launcher.py — open a Terminus tab that starts the PyBackup Flask app.

Menu: Main.sublime-menu -> Tools -- "PyBackup Flask App"
Command palette: "PyBackup: Flask App"
"""

import os
import socket
import subprocess
import time

import sublime
import sublime_plugin

PORT = 5757


def _pids_listening_on_windows(port: int) -> list:
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"], stderr=subprocess.DEVNULL, text=True
        )
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local, state, pid = parts[1], parts[3], parts[4]
        if state != "LISTENING":
            continue
        host, _, p = local.rpartition(":")
        if p != str(port):
            continue
        if pid.isdigit():
            pids.append(int(pid))
    return pids


def _pids_listening_on_posix(port: int) -> list:
    try:
        out = subprocess.check_output(
            ["lsof", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
            stderr=subprocess.DEVNULL, text=True,
        )
    except Exception:
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def kill_existing(port: int = PORT, timeout: float = 3.0) -> int:
    """Kill any process listening on `port`; wait for the port to free.

    Returns the number of processes killed. Safe to call when nothing is
    listening (returns 0). Needed because a stale prior instance (still
    holding cached engine state) would otherwise serve the UI instead of
    the freshly spawned process.
    """
    if os.name == "nt":
        pids = _pids_listening_on_windows(port)
        for pid in pids:
            subprocess.call(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    else:
        pids = _pids_listening_on_posix(port)
        for pid in pids:
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    # Wait for the port to actually free so the new bind doesn't collide.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.25)
        try:
            s.bind(("127.0.0.1", port))
            s.close()
            break
        except OSError:
            s.close()
            time.sleep(0.1)
    return len(pids)


class PbFlaskLauncherCommand(sublime_plugin.WindowCommand):
    """Open a Terminus tab that starts the PyBackup Flask app and launches its browser UI.

    Kills any prior process still holding port %d first, so a stale instance
    with cached engine state can't serve the freshly launched UI.

    Menu: Main.sublime-menu -> Tools -- "PyBackup Flask App"
    Command palette: "PyBackup: Flask App"
    """ % PORT

    def run(self):
        kill_existing(PORT)
        self.window.run_command(
            "terminus_open",
            {
                "title": "Pybackup Flask App",
                "tag": "pb_flask",
                "post_view_hooks": [
                    [
                        "terminus_paste_text",
                        {
                            "text": "start http://127.0.0.1:%d\n" % PORT,
                            "bracketed": False,
                        },
                    ],
                    [
                        "terminus_paste_text",
                        {
                            "text": "start /B \"C:\\Users\\donal\\AppData\\Local\\Programs\\Python\\Python312\\python.exe\" C:/Users/donal/projects/pybackup/ui/app.py\n",
                            "bracketed": False,
                        },
                    ],
                ],
            },
        )
