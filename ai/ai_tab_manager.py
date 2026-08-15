"""ai_tab_manager.py — Session browsing and Flask app management for Ai.

Logging lives in the STLogs package. Terminal management lives in GhostShell.
"""

import calendar
import datetime
import json
import time
from pathlib import Path

import sublime  # type: ignore
import sublime_plugin  # type: ignore


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
    """List recent Claude Code sessions across all projects in a scratch view.

    Command palette: "Ai: List Recent Sessions"
    """

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


# -- flask management ---------------------------------------------------------

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


# -- lifecycle -----------------------------------------------------------------

def plugin_loaded():
    print("ai_tab_manager: loaded")


def plugin_unloaded():
    print("ai_tab_manager: unloaded")
