"""ai_tab_manager.py — Buffer management for Ai sessions in Sublime Text.

PURPOSE
=======
Keeps the Terminus "Ai" view buffer trimmed to a manageable size while
preserving content in the log before deletion.

Logging lives in ai_logger.py.  Only minimal log-append helpers are
duplicated here so AiTrimNowCommand can record content before deleting it.
"""

import calendar
import datetime
import json
import math
import os
import subprocess
import time
from pathlib import Path

import sublime  # type: ignore
import sublime_plugin  # type: ignore


# -- minimal log helpers (duplicated from ai_logger.py for independence) ------

_LOG_DIR = str(Path.home() / ".claude" / "conversation_logs")
_DIAGNOSTICS_FILE = str(Path.home() / ".claude" / "ai_diagnostics.log")


def _diagnostic_log(message: str) -> None:
    try:
        os.makedirs(os.path.dirname(_DIAGNOSTICS_FILE), exist_ok=True)
        with open(_DIAGNOSTICS_FILE, "a", encoding="utf-8") as f:
            timestamp = datetime.datetime.now().isoformat()
            f.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass


def _append_log(text: str, session_tag: str = "") -> None:
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        log_file = os.path.join(_LOG_DIR, f"ai_{datetime.date.today().isoformat()}.log")
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        header = f"\n[{ts}{(' ' + session_tag) if session_tag else ''}]\n"
        with open(log_file, "a", encoding="utf-8", newline="") as f:
            f.write(header + text)
    except OSError as e:
        _diagnostic_log(f"WRITE_ERROR: {e}")


# -- view detection -----------------------------------------------------------
# Also present in ai_logger.py for that module's independence.

def _get_child_process_names() -> dict:
    """Return a dict mapping parent_pid -> list of child exe names (lowercase).
    Uses Windows CreateToolhelp32Snapshot. Returns {} on non-Windows or error.
    """
    child_map: dict = {}
    try:
        import ctypes
        import ctypes.wintypes

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.wintypes.DWORD),
                ("cntUsage", ctypes.wintypes.DWORD),
                ("th32ProcessID", ctypes.wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", ctypes.wintypes.DWORD),
                ("cntThreads", ctypes.wintypes.DWORD),
                ("th32ParentProcessID", ctypes.wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        snap = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if ctypes.windll.kernel32.Process32First(snap, ctypes.byref(entry)):
            while True:
                ppid = entry.th32ParentProcessID
                name = entry.szExeFile.decode("utf-8", errors="replace").lower()
                child_map.setdefault(ppid, []).append(name)
                if not ctypes.windll.kernel32.Process32Next(snap, ctypes.byref(entry)):
                    break
        ctypes.windll.kernel32.CloseHandle(snap)
    except Exception:
        pass
    return child_map


def _claude_views():
    """Return all Terminus views with a live claude process, with session info."""
    results = []
    try:
        from Terminus.terminus.terminal import Terminal  # type: ignore
    except ImportError:
        return results

    child_map = _get_child_process_names()

    for w in sublime.windows():
        for v in w.views():
            t = Terminal.from_id(v.id())
            if not t or not t.process:
                continue
            try:
                argv0 = t.process.argv[0] if t.process.argv else ""
                alive = t.process.isalive()
            except Exception:
                continue
            if not alive:
                continue
            pid = t.process.pid
            is_claude = "claude" in argv0.lower()
            if not is_claude:
                is_claude = any("claude" in c for c in child_map.get(pid, []))
            if is_claude:
                name = v.name() or f"view{v.id()}"
                results.append((v, pid, name))
    return results


def _is_ai_view(v):
    """Return True if v is a tracked claude Terminus view."""
    try:
        from Terminus.terminus.terminal import Terminal
        t = Terminal.from_id(v.id())
        return bool(t and t.process and "claude" in (t.process.argv[0] if t.process.argv else "").lower())
    except Exception:
        return False


def _ai_view():
    """Return the primary claude Terminus view, or None."""
    views = _claude_views()
    return views[0][0] if views else None


# -- core commands ------------------------------------------------------------

class AiTrimNowCommand(sublime_plugin.TextCommand):
    """Manually trim buffer to scrollback_history_size, logging content first."""

    def run(self, edit):
        try:
            from Terminus.terminus.terminal import Terminal  # type: ignore
        except ImportError:
            sublime.error_message("Terminus plugin not found")
            return

        v = self.view
        terminal = Terminal.from_id(v.id())
        if not terminal:
            return

        n = sublime.load_settings("Terminus.sublime-settings").get(
            "scrollback_history_size", 500
        )
        lastrow = v.rowcol(v.size())[0]
        lines_deleted = 0

        while lastrow + 1 > n:
            m = max(lastrow + 1 - n, math.ceil(n / 10))
            top_region = sublime.Region(0, v.line(v.text_point(m - 1, 0)).end() + 1)

            try:
                deleted_text = v.substr(top_region)
                _append_log(deleted_text)
                _diagnostic_log(f"TRIM: Logged and deleted {m} lines")
            except Exception as e:
                _diagnostic_log(f"TRIM_ERROR: {e}")

            v.erase(edit, top_region)
            lines_deleted += m
            terminal.offset = max(0, terminal.offset - m)
            lastrow = v.rowcol(v.size())[0]

        final_lines = v.rowcol(v.size())[0] + 1
        msg = f"ai_tab_manager: trimmed {lines_deleted} lines, now {final_lines} total"
        print(msg)
        _diagnostic_log(msg)


class AiDumpBufferCommand(sublime_plugin.TextCommand):
    """Export the entire current Ai buffer to a file for inspection/archival."""

    def run(self, edit):
        v = self.view
        if not _is_ai_view(v):
            sublime.error_message("This command only works in the Ai view")
            return

        try:
            entire_content = v.substr(sublime.Region(0, v.size()))

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            export_dir = Path.home() / ".claude" / "buffer_exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            export_file = export_dir / f"ai_buffer_dump_{timestamp}.txt"

            with open(export_file, "w", encoding="utf-8") as f:
                f.write(entire_content)

            msg = f"Buffer exported to: {export_file}"
            print(msg)
            sublime.status_message(msg)
            _diagnostic_log(f"BUFFER_DUMP: {export_file} ({len(entire_content)} chars)")

        except Exception as e:
            error_msg = f"Failed to export buffer: {e}"
            print(error_msg)
            _diagnostic_log(f"BUFFER_DUMP_ERROR: {e}")
            sublime.error_message(error_msg)


# -- session browsing ---------------------------------------------------------

def _extract_message_text(payload: dict) -> str:
    """Extract visible text from a Ai response_item message payload."""
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    message = payload.get("message")
    if isinstance(message, dict):
        return _extract_message_text(message)
    return ""


def _read_session_info(jsonl_path: Path) -> dict:
    """Extract first prompt, timestamps, and exchange count from a Claude Code JSONL."""
    first_prompt = None
    first_ts = None
    last_ts = None
    exchanges = 0
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = obj.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

                if obj.get("type") != "user":
                    continue
                msg = obj.get("message") or {}
                if msg.get("role") != "user":
                    continue

                text = _extract_message_text(msg).strip()
                if not text or text.startswith("<"):
                    continue
                exchanges += 1
                if not first_prompt:
                    first_prompt = text[:120].replace("\n", " ")
    except OSError:
        pass
    return {
        "title": jsonl_path.stem,
        "first_prompt": first_prompt,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "exchanges": exchanges,
    }


def _decode_project(folder_name):
    import re
    return re.sub(r'^[A-Z]--Users-[^-]+-', '', folder_name)


class AiListSessionsCommand(sublime_plugin.WindowCommand):
    """Show recent Ai sessions across all projects."""

    def run(self, count=40):
        projects_dir = Path.home() / ".claude" / "projects"
        if not projects_dir.exists():
            sublime.error_message("No ~/.claude/projects directory found")
            return

        sessions = []
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            project = _decode_project(project_dir.name)
            for jsonl in project_dir.glob("*.jsonl"):
                if jsonl.parent != project_dir:
                    continue
                mtime = jsonl.stat().st_mtime
                sessions.append((mtime, project, jsonl))

        sessions.sort(key=lambda x: x[0], reverse=True)
        sessions = sessions[:count]

        lines = [f"Recent Ai sessions (last {count}):\n"]
        for mtime, project, jsonl in sessions:
            info = _read_session_info(jsonl)

            dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
            lines.append(f"{dt}  [{project}]  {info['exchanges']} exchanges")
            lines.append(f"  Title:  {info['title']}")

            if info["first_prompt"]:
                prompt = info["first_prompt"]
                if len(prompt) == 120:
                    prompt += "…"
                lines.append(f"  First:  {prompt}")

            if info["first_ts"] and info["last_ts"]:
                def fmt_ts(ts):
                    try:
                        s = ts.replace("Z", "").split(".")[0]
                        dt_utc = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
                        epoch = calendar.timegm(dt_utc.timetuple())
                        return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
                    except Exception:
                        return ts[:16]
                start = fmt_ts(info["first_ts"])
                end = fmt_ts(info["last_ts"])
                if start == end:
                    lines.append(f"  Time:   {start}")
                else:
                    lines.append(f"  Time:   {start} → {end}")

            lines.append("")

        output = "\n".join(lines)
        v = self.window.new_file()
        v.set_name("Ai Sessions")
        v.set_scratch(True)
        v.run_command("append", {"characters": output})


class AiSearchConversationsCommand(sublime_plugin.WindowCommand):
    """Launch the Ai conversation search Flask app in a browser."""

    def run(self):
        import socket
        import webbrowser
        url = "http://127.0.0.1:5758"

        def _port_free(p):
            with socket.socket() as s:
                try:
                    s.connect(("127.0.0.1", p))
                    return False
                except OSError:
                    return True

        if _port_free(5758):
            script = str(Path(__file__).parent / "ai_search_app.py")
            subprocess.Popen(
                ["python", script],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            webbrowser.open(url)


# -- flask management ---------------------------------------------------------

_FLASK_APPS = [
    ("ai_search_app",  5758, "GET",  "/quit"),
    ("pybackup",       5757, "POST", "/api/shutdown"),
    ("blog7",          5000, "GET",  "/quit"),
    ("finance",        5050, "GET",  "/quit"),
]


class AiQuitFlaskAppsCommand(sublime_plugin.WindowCommand):
    """Quit all running Flask apps. Command palette: Ai: Quit Flask Apps"""

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


# -- event listener (gutter settings only) ------------------------------------

class AiEventListener(sublime_plugin.EventListener):
    """Enable line numbers/gutter on Terminus views."""

    def on_load(self, view: sublime.View) -> None:
        if view.settings().get('terminus_view'):
            view.settings().set('gutter', True)
            view.settings().set('line_numbers', True)

    def on_activated(self, view: sublime.View) -> None:
        if view.settings().get('terminus_view') and not view.settings().get('gutter'):
            view.settings().set('gutter', True)
            view.settings().set('line_numbers', True)


# -- lifecycle -----------------------------------------------------------------

def plugin_loaded():
    os.makedirs(_LOG_DIR, exist_ok=True)
    print("ai_tab_manager: loaded")


def plugin_unloaded():
    print("ai_tab_manager: unloaded")
