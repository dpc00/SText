"""Cross-platform shims for Windows-only process/desktop APIs.

Sublime's plugin_host runs the same Packages/User tree on Windows and Linux.
Several launchers were written against `subprocess.STARTUPINFO`,
`subprocess.CREATE_NO_WINDOW` and `os.startfile`, none of which exist off
Windows -- referencing them at import time killed the whole plugin load.
"""

import os
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"


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
        print("winutil.open_url: %s failed: %s" % (opener, e))
