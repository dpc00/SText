"""Shared kill-before-launch helper for PyBackup Flask launchers.

Kills any python process currently listening on the Flask port so a stale
prior instance (still holding cached engine state) doesn't serve the UI
instead of the freshly spawned process. Cross-process on Windows via netstat
+ taskkill; no-ops cleanly on non-Windows or if nothing is listening.
"""

import os
import socket
import subprocess
import time

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
    listening (returns 0).
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