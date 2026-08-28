"""ai_quit_flask_apps.py — send each known Flask app's shutdown endpoint to stop them.

Command palette: "Ai: Quit Flask Apps"
"""

import sublime  # type: ignore
import sublime_plugin  # type: ignore

_FLASK_APPS = [
    ("ai_search_app",  5758, "POST", "/close"),
    ("pybackup",       5757, "POST", "/api/shutdown"),
    ("blog7",          5000, "GET",  "/quit"),
    ("finance",        5050, "GET",  "/quit"),
]


class AiQuitFlaskAppsCommand(sublime_plugin.WindowCommand):
    """Send each known Flask app's shutdown endpoint to stop them.

    Command palette: "Ai: Quit Flask Apps"
    """

    def run(self):
        import urllib.request
        import urllib.error
        killed = []
        for name, port, method, path in _FLASK_APPS:
            try:
                url = f"http://127.0.0.1:{port}{path}"
                data = b"{}" if method == "POST" else None
                req = urllib.request.Request(url, data=data, method=method)
                if data is not None:
                    req.add_header("Content-Type", "application/json")
                urllib.request.urlopen(req, timeout=2)
                killed.append(name)
            except urllib.error.URLError:
                pass
            except Exception:
                killed.append(name)
        msg = f"Quit: {', '.join(killed)}" if killed else "No Flask apps were running"
        sublime.status_message(msg)


def plugin_loaded():
    print("ai_quit_flask_apps: loaded")


def plugin_unloaded():
    print("ai_quit_flask_apps: unloaded")
