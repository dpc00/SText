"""ai_settings.py — ST Settings browser (Flask edition).

Opens with Ctrl+Alt+, (comma).  Launches a local web server and opens the
settings UI in the default browser.  Real HTML form controls, full-page
scrolling, no phantom limitations.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import sublime
import sublime_plugin

# ── known enum options ─────────────────────────────────────────────────────────

ENUMS = {
    "caret_style":                  ["smooth", "phase", "blink", "wide", "solid"],
    "auto_complete_preserve_order": ["none", "some", "strict"],
    "default_line_ending":          ["system", "windows", "unix"],
    "control_character_style":      ["hex", "abbreviation", "replacement"],
    "mini_diff":                    ["true", "false", "auto"],
    "word_wrap":                    ["true", "false", "auto"],
    "show_git_status":              ["true", "false", "auto"],
    "highlight_modified_tabs":      ["true", "false", "auto"],
    "draw_white_space":             ["none", "selection", "leading", "enclosed", "trailing", "isolated", "all"],
}

# ── categories ────────────────────────────────────────────────────────────────

CATEGORIES = {
    "Font & Display": [
        "font_face", "font_size", "font_options", "line_numbers", "gutter",
        "margin", "fold_buttons", "fade_fold_buttons", "rulers",
        "draw_minimap_border", "always_show_minimap_viewport",
        "draw_white_space", "draw_unicode_white_space", "draw_indent_guides",
        "indent_guide_options", "highlight_line", "caret_style",
        "caret_extra_top", "caret_extra_bottom", "caret_extra_width",
        "block_caret", "animation_enabled",
    ],
    "Editor Behavior": [
        "tab_size", "translate_tabs_to_spaces", "use_tab_stops",
        "auto_indent", "smart_indent", "indent_to_bracket",
        "trim_trailing_white_space_on_save", "ensure_newline_at_eof_on_save",
        "default_line_ending", "word_wrap", "wrap_width",
        "word_separators", "detect_indentation",
    ],
    "Autocomplete": [
        "auto_complete", "auto_complete_delay", "auto_complete_commit_on_tab",
        "auto_complete_cycle", "auto_complete_use_history",
        "auto_complete_use_index", "auto_complete_preserve_order",
        "auto_complete_with_fields", "tab_completion",
        "auto_match_enabled", "auto_close_tags",
    ],
    "Files & Save": [
        "hot_exit", "remember_open_files", "always_prompt_for_file_reload",
        "atomic_save", "backup_on_save", "create_window_at_startup",
        "save_on_focus_lost", "close_windows_when_empty",
        "default_encoding", "fallback_encoding",
        "binary_file_patterns", "file_exclude_patterns",
        "folder_exclude_patterns",
    ],
    "UI": [
        "theme", "color_scheme", "dark_color_scheme", "light_color_scheme",
        "show_tabs", "enable_tab_scrolling", "show_encoding",
        "show_indentation", "show_line_endings", "show_sidebar",
        "sidebar_no_dir_prefix", "bold_folder_labels",
        "mouse_wheel_switches_tabs", "auto_hide_tabs", "auto_hide_menu",
        "auto_hide_status_bar", "adaptive_dividers",
        "mini_diff", "show_definitions",
    ],
    "Spell Check": [
        "spell_check", "dictionary", "added_words", "ignored_words",
    ],
    "Performance": [
        "index_files", "index_workers", "index_exclude_patterns",
        "scroll_speed", "tree_animation_enabled",
    ],
}

PREFS_RESOURCE = "Packages/Default/Preferences.sublime-settings"

# ── description parser ─────────────────────────────────────────────────────────

_DESCRIPTIONS = {}


def _get_descriptions():
    global _DESCRIPTIONS
    if _DESCRIPTIONS:
        return _DESCRIPTIONS
    try:
        raw = sublime.load_resource(PREFS_RESOURCE)
        pending = []
        for line in raw.splitlines():
            s = line.strip()
            if s.startswith("//"):
                pending.append(s[2:].strip())
            elif s.startswith('"'):
                m = re.match(r'"(\w+)"\s*:', s)
                if m:
                    desc = " ".join(pending).strip()
                    _DESCRIPTIONS[m.group(1)] = desc
                    pending = []
            else:
                if s not in ("{", "}"):
                    pending = []
    except Exception:
        pass
    return _DESCRIPTIONS


# ── utilities ─────────────────────────────────────────────────────────────────

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _find_python():
    import subprocess as _sp
    candidates = []
    # Known install paths first (avoid Microsoft Store stubs)
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    for ver in ("Python313", "Python312", "Python311", "Python310", "Python39"):
        p = local / "Programs" / "Python" / ver / "python.exe"
        candidates.append(str(p))
    # PATH fallbacks — skip WindowsApps stubs
    for name in ("python", "python3"):
        path = shutil.which(name)
        if path and "WindowsApps" not in path:
            candidates.append(path)
    for c in candidates:
        if not os.path.isfile(c):
            continue
        try:
            r = _sp.run([c, "--version"], capture_output=True, timeout=3)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return sys.executable



def _build_data(settings_resource):
    """Collect all settings data into a dict for the browser."""
    try:
        raw = sublime.load_resource(settings_resource)
        defaults = sublime.decode_value(raw)
    except Exception:
        defaults = {}

    settings_fname = settings_resource.split("/")[-1]
    user_resource = f"Packages/User/{settings_fname}"
    try:
        user_raw = sublime.load_resource(user_resource)
        user_prefs = sublime.decode_value(user_raw)
    except Exception:
        user_prefs = {}

    descs = _get_descriptions()

    categorised = {k for ks in CATEGORIES.values() for k in ks}
    other = [k for k in defaults if k not in categorised]
    show_order = list(CATEGORIES.items())
    if other:
        show_order.append(("Other", other))

    live = sublime.load_settings(settings_fname)
    return {
        "defaults": defaults,
        "user_prefs": user_prefs,
        "effective": {"font_face": live.get("font_face", "")},
        "descriptions": {k: descs.get(k, "") for k in defaults},
        "enums": ENUMS,
        "categories": {k: list(v) for k, v in CATEGORIES.items()},
        "show_order": [[s, ks] for s, ks in show_order],
        "settings_fname": settings_fname,
    }


# ── callback HTTP server (receives changes from browser) ──────────────────────

_callback_server = None
_callback_port = None


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n))

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            body = self._read_json()
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return

        path = self.path.split("?")[0]
        key = body.get("key", "")
        settings_fname = body.get("settings_fname", "Preferences.sublime-settings")

        if path == "/apply":
            value = body.get("value")
            def apply():
                try:
                    s = sublime.load_settings(settings_fname)
                    s.set(key, value)
                    sublime.save_settings(settings_fname)
                except Exception as e:
                    print(f"ai_settings callback apply error: {e}")
            sublime.set_timeout(apply, 0)
            self._send_json({"ok": True})

        elif path == "/reset":
            def reset():
                try:
                    s = sublime.load_settings(settings_fname)
                    s.erase(key)
                    sublime.save_settings(settings_fname)
                except Exception as e:
                    print(f"ai_settings callback reset error: {e}")
            sublime.set_timeout(reset, 0)
            self._send_json({"ok": True})

        else:
            self._send_json({"ok": False, "error": "unknown"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def _ensure_callback_server():
    global _callback_server, _callback_port
    if _callback_server is not None:
        return _callback_port
    _callback_port = _free_port()
    _callback_server = HTTPServer(("127.0.0.1", _callback_port), _CallbackHandler)
    t = threading.Thread(target=_callback_server.serve_forever, daemon=True)
    t.start()
    return _callback_port


# ── subprocess / browser launcher ─────────────────────────────────────────────

_FIXED_PORT = 57321
_browser_proc = None
_data_file = None
_server_url = None
_gen = 0


def _launch(settings_resource=PREFS_RESOURCE):
    global _browser_proc, _data_file, _server_url, _gen

    def _port_free(p):
        with socket.socket() as s:
            try: s.bind(("127.0.0.1", p)); return True
            except OSError: return False

    # Kill our tracked process, then kill anything else still on the port
    if _browser_proc and _browser_proc.poll() is None:
        _browser_proc.terminate()

    if not _port_free(_FIXED_PORT):
        try:
            import subprocess as _sp
            _sp.run(
                f'for /f "tokens=5" %a in (\'netstat -aon ^| findstr :{_FIXED_PORT}\') do taskkill /f /pid %a',
                shell=True, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL
            )
        except Exception:
            pass

    import time
    for _ in range(20):
        if _port_free(_FIXED_PORT):
            break
        time.sleep(0.1)

    _gen += 1
    callback_port = _ensure_callback_server()
    server_port = _FIXED_PORT

    data = _build_data(settings_resource)

    tmp = Path(sublime.packages_path()).parent / "ai_settings_data.json"
    tmp.write_text(json.dumps(data), encoding="utf-8")
    _data_file = str(tmp)

    server_script = os.path.join(
        sublime.packages_path(), "User", "ai_settings_server.py"
    )
    python = _find_python()

    _browser_proc = subprocess.Popen(
        [python, server_script,
         "--data-file", _data_file,
         "--callback", f"http://127.0.0.1:{callback_port}",
         "--port", str(server_port),
         "--gen", str(_gen)],
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )

    url = f"http://127.0.0.1:{server_port}"
    _server_url = url

    def open_browser():
        time.sleep(1.5)
        import webbrowser
        webbrowser.open(url)
    threading.Thread(target=open_browser, daemon=True).start()

    sublime.status_message(f"ST Settings: {url}")


# ── commands ──────────────────────────────────────────────────────────────────

class AiSettingsOpenCommand(sublime_plugin.WindowCommand):
    """Open the ST Settings browser in default browser.  Ctrl+Alt+."""

    def run(self):
        import threading
        threading.Thread(target=_launch, daemon=True).start()
