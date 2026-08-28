"""pb_flask_launcher_silent.py — start the PyBackup Flask app headlessly and open its browser UI.

Menu: Main.sublime-menu -> Tools -- "PyBackup Flask App (Silent)"
Command palette: "PyBackup: Flask App (silent)"
"""

import os
import socket
import subprocess
import sys
import threading
import time

import sublime
import sublime_plugin

PORT = 5757
IS_WINDOWS = sys.platform == "win32"


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


def hidden_popen_kwargs():
    """Popen kwargs that suppress a console window, empty off-Windows."""
    if not IS_WINDOWS:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": si,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
    }


def open_url(url):
    """Open a URL in the desktop default browser."""
    if IS_WINDOWS:
        os.startfile(url)
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen(
            [opener, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        print("pb_flask_launcher_silent.open_url: %s failed: %s" % (opener, e))


def assign_pid(pid):
    """Assign a running child pid to the ST-lifetime Windows Job Object, so the
    child is killed automatically when ST exits instead of being orphaned.

    Uses a job handle stashed on `sys` (shared across every module that
    duplicates this helper, and across plugin reloads, since `sys` itself
    survives both). No-op off-Windows.
    """
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    job = getattr(sys, "_st_win_job_handle", None)
    if job is None:
        h = k32.CreateJobObjectW(None, None)
        if not h:
            print("pb_flask_launcher_silent: CreateJobObjectW failed: %d" % ctypes.get_last_error())
            sys._st_win_job_handle = False
            return
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
            h, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        ):
            print("pb_flask_launcher_silent: SetInformationJobObject failed: %d" % ctypes.get_last_error())
            k32.CloseHandle(h)
            sys._st_win_job_handle = False
            return
        sys._st_win_job_handle = h
        job = h

    if job is False:
        return

    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    hproc = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not hproc:
        return
    try:
        k32.AssignProcessToJobObject(job, hproc)
    finally:
        k32.CloseHandle(hproc)


def _wait_and_open(port, timeout=60):
    """Poll until Flask is accepting connections, then open the browser."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.25)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            open_url("http://127.0.0.1:%d" % port)
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

    Menu: Main.sublime-menu -> Tools -- "PyBackup Flask App (Silent)"
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
            proc = subprocess.Popen(
                [r"C:\Users\donal\AppData\Local\Programs\Python\Python312\python.exe", r"C:/Users/donal/projects/pybackup/ui/app.py"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                **hidden_popen_kwargs()
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
