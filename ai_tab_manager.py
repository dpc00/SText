"""ai_tab_manager.py ” robust logging for Ai sessions in Sublime Text.

PURPOSE
=======
The Terminus view named "Ai" is the live Ai session. This plugin
creates multiple layers of logging to capture EVERYTHING Ai does, because
Ai itself fails to persistently log many operations (especially agent
spawning).

LOGGING LAYERS
==============
1. Buffer logging: Incremental capture of all lines written to the Terminus buffer
   - Line-based tracking (survives buffer trimming without data loss)
   - Persistent state saved to disk (survives plugin reloads)
   - Fast polling (500ms) to catch rapid output

2. Data loss detection: Monitors for gaps and anomalies
   - Detects when buffer shrinks unexpectedly (lines deleted without logging)
   - Detects when output pauses (might indicate invisible operations)
   - Alerts user when data loss is detected

3. Screenshot backup: Periodic screen captures
   - Captures current state at intervals
   - Acts as visual evidence for UI-rendered content (like agent lists)
   - Preserves what's visible even if not in the buffer

4. Diagnostic reporting: Auto-generates incident reports
   - Detects when large numbers of agents might have spawned
   - Creates incident reports for bug submission
   - Tracks costs, timing, and anomalies

STATE
=====
- Per-view state (last-logged line, view size, etc.) saved to disk
- Survives plugin reloads, Sublime restarts, and view closures
- State file: ~/.claude/ai_tab_manager_state.json
"""

import datetime
import json
import math
import os
import subprocess
import threading
from pathlib import Path

import sublime  # type: ignore
import sublime_plugin  # type: ignore

_LOG_DIR = str(Path.home() / ".claude" / "conversation_logs")
_STATE_FILE = str(Path.home() / ".claude" / "ai_tab_manager_state.json")
_SCREENSHOT_DIR = str(Path.home() / ".claude" / "screenshots")
_DIAGNOSTICS_FILE = str(Path.home() / ".claude" / "ai_diagnostics.log")
_CHECK_MS = 500  # poll every 500ms (was 4000ms)
_SCREENSHOT_INTERVAL = 60  # capture screenshot every 60 seconds
_SCREENSHOT_RETENTION_DAYS = 7  # keep screenshots for 7 days, delete older ones
_AI_VIEW_SETTING = "ai_logger"

# In-memory cache of per-view state
_view_state = {}  # view_id -> {last_line, last_checked_time, anomalies, etc}
_last_screenshot_time = {}  # view_id -> timestamp
_last_cleanup_time = 0  # timestamp of last cleanup run


def _load_state():
    """Load state from disk."""
    global _view_state
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                _view_state = json.load(f)
    except (OSError, json.JSONDecodeError):
        _view_state = {}


def _save_state():
    """Persist state to disk."""
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_view_state, f, indent=2)
    except OSError as e:
        print(f"ai_tab_manager: ERROR saving state: {e}")


def _diagnostic_log(message: str) -> None:
    """Write to diagnostic log for debugging."""
    try:
        os.makedirs(os.path.dirname(_DIAGNOSTICS_FILE), exist_ok=True)
        with open(_DIAGNOSTICS_FILE, "a", encoding="utf-8") as f:
            timestamp = datetime.datetime.now().isoformat()
            f.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass


def _cleanup_old_screenshots() -> None:
    """Delete screenshots older than _SCREENSHOT_RETENTION_DAYS to manage disk space."""
    try:
        import time

        screenshot_dir = Path(_SCREENSHOT_DIR)
        if not screenshot_dir.exists():
            return

        cutoff_time = time.time() - (_SCREENSHOT_RETENTION_DAYS * 86400)
        deleted_count = 0
        total_freed_bytes = 0

        for screenshot_file in screenshot_dir.glob("*.png"):
            if os.path.getmtime(screenshot_file) < cutoff_time:
                try:
                    size = os.path.getsize(screenshot_file)
                    os.remove(screenshot_file)
                    deleted_count += 1
                    total_freed_bytes += size
                except OSError:
                    pass

        if deleted_count > 0:
            freed_mb = total_freed_bytes / (1024 * 1024)
            _diagnostic_log(
                f"CLEANUP: Deleted {deleted_count} old screenshots, freed {freed_mb:.1f}MB"
            )
    except Exception as e:
        _diagnostic_log(f"CLEANUP_ERROR: {e}")


import re as _re

_TRAIL_JUNK = _re.compile("[\s─-╿▀-▟]+$")
# Ai status-bar lines: wide single lines containing navigation/cost info
# Status-bar lines are padded to terminal width: non-space, big gap, non-space
_STATUS_BAR_GAP = _re.compile(r"\S\s{20,}\S")


def _clean_text(text: str) -> str:
    """Normalize terminal buffer text for clean log output."""
    # Replace non-breaking space with regular space (Terminus pads with \xa0)
    text = text.replace("\xa0", " ")
    # Remove zero-width invisible characters
    text = (
        text.replace("​", "").replace("‌", "").replace("‍", "").replace("﻿", "")
    )
    # Normalize line endings: \r\n → \n, then bare \r → \n
    # Bare \r is used by terminals to overwrite the current line; treating as \n
    # splits each overwrite fragment so the status-bar filter can catch them.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        line = _TRAIL_JUNK.sub("", line)
        # Drop terminal status-bar lines (wide lines padded between left/right elements)
        if len(line) > 100 and _STATUS_BAR_GAP.search(line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def _append_log(text: str) -> None:
    """Append text to today's conversation log."""
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        log_file = os.path.join(_LOG_DIR, f"ai_{datetime.date.today().isoformat()}.log")
        with open(log_file, "a", encoding="utf-8", newline="") as f:
            f.write(_clean_text(text))
    except OSError as e:
        _diagnostic_log(f"WRITE_ERROR: Failed to write to log: {e}")


def _is_ai_view(view: sublime.View) -> bool:
    """Return whether a Sublime view is the Ai Terminus session."""
    if view.name() == "Ai":
        return True
    if view.settings().get(_AI_VIEW_SETTING):
        return True
    return False


def _ai_view():
    """Return the first Ai Terminus view across all windows, or None."""
    for w in sublime.windows():
        for v in w.views():
            if _is_ai_view(v):
                return v
    return None


def _take_screenshot(view_id: str) -> None:
    """Capture screenshot as backup evidence of what's visible on screen."""
    try:
        os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(_SCREENSHOT_DIR, f"ai_{timestamp}.png")

        # Use PIL to capture full screen (preferred)
        try:
            from PIL import ImageGrab

            screenshot = ImageGrab.grab()
            screenshot.save(filepath, "PNG")
            _diagnostic_log(f"SCREENSHOT: Captured to {filepath}")
        except ImportError:
            # Fallback: use PowerShell screencap (hidden window)
            ps_script = f"""
$image = [Windows.Graphics.Capture.GraphicsCaptureSession]::IsSupported()
Add-Type -AssemblyName System.Windows.Forms
$screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bitmap = New-Object System.Drawing.Bitmap($screen.Width, $screen.Height)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($screen.Location, [System.Drawing.Point]::Empty, $screen.Size)
$bitmap.Save("{filepath}")
$graphics.Dispose()
$bitmap.Dispose()
"""
            # Hide the PowerShell window
            import subprocess

            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

            subprocess.run(
                ["powershell", "-WindowStyle", "Hidden", "-Command", ps_script],
                capture_output=True,
                timeout=10,
                startupinfo=startupinfo,
            )
            _diagnostic_log(f"SCREENSHOT: Captured to {filepath}")
    except Exception as e:
        _diagnostic_log(f"SCREENSHOT_ERROR: {e}")


def _detect_anomalies(
    view_id: str, current_row_count: int, last_row_count: int
) -> None:
    """Detect unusual patterns that might indicate lost data or invisible operations."""
    if view_id not in _view_state:
        return

    import time

    state = _view_state[view_id]
    now = time.time()

    # Detect if buffer shrunk (trim/reload) ” reset last_line so we re-log current buffer
    if current_row_count < last_row_count:
        lost_lines = last_row_count - current_row_count
        if lost_lines > 5:
            _diagnostic_log(
                f"ANOMALY: {lost_lines} lines deleted from buffer (resetting last_line to re-log)"
            )
            state["anomalies"] = state.get("anomalies", 0) + 1
            state["last_line"] = 0

    # Detect long pauses ” debounced to once per 5 minutes to avoid log flood
    if "last_output_time" in state:
        time_since_output = now - state["last_output_time"]
        last_pause_logged = state.get("last_pause_logged_time", 0)
        if time_since_output > 30 and now - last_pause_logged > 300:
            _diagnostic_log(
                f"ANOMALY: 30+ seconds without output (possible invisible operation)"
            )
            state["anomalies"] = state.get("anomalies", 0) + 1
            state["last_pause_logged_time"] = now


def _tick():
    """Main polling loop: check for new content every _CHECK_MS milliseconds."""
    global _last_cleanup_time
    import time

    # Periodically cleanup old screenshots (every 2.5 hours)
    current_time = time.time()
    if current_time - _last_cleanup_time > 9000:  # 2.5 hours
        _cleanup_old_screenshots()
        _last_cleanup_time = current_time

    v = _ai_view()

    if v:
        vid = str(v.id())
        row_count = v.rowcol(v.size())[0] + 1

        # Initialize state for this view
        if vid not in _view_state:
            _view_state[vid] = {
                "last_line": 0,
                "last_row_count": row_count,
                "started_at": datetime.datetime.now().isoformat(),
                "anomalies": 0,
            }

        state = _view_state[vid]
        last_line = state.get("last_line", 0)
        last_row_count = state.get("last_row_count", 0)

        # If last_line is stale (e.g. loaded from disk for a resumed view), reset so we
        # re-log whatever is currently in the buffer rather than silently missing content.
        if last_line > row_count:
            _diagnostic_log(
                f"RESET: last_line={last_line} > row_count={row_count}, resetting to 0"
            )
            state["last_line"] = 0
            last_line = 0

        # Detect anomalies
        _detect_anomalies(vid, row_count, last_row_count)
        state["last_row_count"] = row_count

        # Log new lines if they exist
        if row_count > last_line:
            try:
                start_point = (
                    v.text_point(last_line, 0) if last_line < row_count else v.size()
                )
                end_point = v.size()

                if start_point < end_point:
                    new_text = v.substr(sublime.Region(start_point, end_point))
                    _append_log(new_text)

                    # Update state
                    state["last_line"] = row_count
                    state["last_output_time"] = __import__("time").time()
                    _save_state()
            except Exception as e:
                _diagnostic_log(f"LOG_ERROR: Failed to log lines: {e}")

        # Periodic screenshot (every _SCREENSHOT_INTERVAL seconds)
        import time

        current_time = time.time()
        if vid not in _last_screenshot_time:
            _last_screenshot_time[vid] = current_time
        elif current_time - _last_screenshot_time[vid] > _SCREENSHOT_INTERVAL:
            _last_screenshot_time[vid] = current_time
            # Screenshot in background thread so it doesn't block
            threading.Thread(target=_take_screenshot, args=(vid,), daemon=True).start()

    sublime.set_timeout(_tick, _CHECK_MS)


class AiTrimNowCommand(sublime_plugin.TextCommand):
    """Manually trim buffer while preserving all content in logs."""

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

            # Log before deleting
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

        # Reset view state so next tick logs remaining content
        vid = str(v.id())
        if vid in _view_state:
            _view_state[vid]["last_line"] = 0
            _save_state()

        final_lines = v.rowcol(v.size())[0] + 1
        msg = f"ai_tab_manager: trimmed {lines_deleted} lines, now {final_lines} total"
        print(msg)
        _diagnostic_log(msg)


def plugin_loaded():
    """Initialize the plugin when Sublime loads."""
    _load_state()
    os.makedirs(_LOG_DIR, exist_ok=True)
    _cleanup_old_screenshots()  # Clean up old screenshots on startup
    sublime.set_timeout(_tick, _CHECK_MS)
    msg = f"ai_tab_manager: initialized (polling every {_CHECK_MS}ms, screenshots every {_SCREENSHOT_INTERVAL}s, retention {_SCREENSHOT_RETENTION_DAYS} days)"
    print(msg)
    _diagnostic_log(msg)


class AiCaptureScrollPositionCommand(sublime_plugin.TextCommand):
    """Capture screenshot at current scroll position (for manual reconstruction).

    Use this while scrolling through the buffer to document what line you're viewing.
    Determines the visible region on screen and labels the screenshot with the top line.
    """

    def run(self, edit):
        v = self.view
        if not _is_ai_view(v):
            sublime.error_message("This command only works in the Ai view")
            return

        try:
            # Get the first visible line in the current view (based on visible region)
            visible_region = v.visible_region()
            first_visible_point = visible_region.begin()
            row, col = v.rowcol(first_visible_point)

            os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = os.path.join(
                _SCREENSHOT_DIR, f"ai_scroll_line{row+1:04d}_{timestamp}.png"
            )

            try:
                from PIL import ImageGrab

                screenshot = ImageGrab.grab()
                screenshot.save(filepath, "PNG")
            except ImportError:
                # Fallback to PowerShell
                ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
$screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bitmap = New-Object System.Drawing.Bitmap($screen.Width, $screen.Height)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($screen.Location, [System.Drawing.Point]::Empty, $screen.Size)
$bitmap.Save("{filepath}")
$graphics.Dispose()
$bitmap.Dispose()
"""
                import subprocess

                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                subprocess.run(
                    ["powershell", "-WindowStyle", "Hidden", "-Command", ps_script],
                    capture_output=True,
                    timeout=10,
                    startupinfo=startupinfo,
                )

            msg = f"Screenshot captured at line {row+1}: {filepath}"
            print(msg)
            sublime.status_message(msg)
            _diagnostic_log(f"MANUAL_SCREENSHOT: {filepath} (line {row+1})")

        except Exception as e:
            error_msg = f"Failed to capture screenshot: {e}"
            print(error_msg)
            _diagnostic_log(f"SCREENSHOT_ERROR: {e}")
            sublime.error_message(error_msg)


class AiDumpBufferCommand(sublime_plugin.TextCommand):
    """Export the entire current Ai buffer to a file for inspection/archival."""

    def run(self, edit):
        v = self.view
        if not _is_ai_view(v):
            sublime.error_message("This command only works in the Ai view")
            return

        try:
            # Get entire buffer content
            entire_content = v.substr(sublime.Region(0, v.size()))

            # Save to dated file
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


class AiEventListener(sublime_plugin.EventListener):
    """Flush any unlogged Ai buffer content when the view or window closes."""

    def _flush_ai_view(self, view: sublime.View) -> None:
        if not _is_ai_view(view):
            return
        try:
            vid = str(view.id())
            row_count = view.rowcol(view.size())[0] + 1
            last_line = _view_state.get(vid, {}).get("last_line", 0)
            if last_line >= row_count:
                return  # nothing new since last tick
            start_point = view.text_point(last_line, 0)
            end_point = view.size()
            if start_point >= end_point:
                return
            new_text = view.substr(sublime.Region(start_point, end_point))
            if not new_text.strip():
                return
            _append_log(new_text)
            if vid in _view_state:
                _view_state[vid]["last_line"] = row_count
            _save_state()
            _diagnostic_log(f"CLOSE_FLUSH: saved {len(new_text)} chars from Ai view")
        except Exception as e:
            _diagnostic_log(f"CLOSE_FLUSH_ERROR: {e}")

    def on_pre_close(self, view: sublime.View) -> None:
        self._flush_ai_view(view)

    def on_window_command(self, window, command_name, args):
        if command_name in ("close_window", "exit"):
            v = _ai_view()
            if v:
                self._flush_ai_view(v)


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
    """Extract project, first prompt, timestamps, and exchange count."""
    project = "(unknown)"
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

                payload = obj.get("payload") or {}
                if obj.get("type") == "session_meta":
                    project = payload.get("cwd") or project
                    continue

                if obj.get("type") != "response_item":
                    continue
                if payload.get("type") != "message" or payload.get("role") != "user":
                    continue

                text = _extract_message_text(payload).strip()
                if not text or text.startswith("<"):
                    continue
                exchanges += 1
                if not first_prompt:
                    first_prompt = text[:120].replace("\n", " ")
    except OSError:
        pass
    return {
        "title": jsonl_path.stem,
        "project": project,
        "first_prompt": first_prompt,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "exchanges": exchanges,
    }


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
            info = _read_session_info(str(jsonl))

            # Header: date + project + exchange count
            dt = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            lines.append(f"{dt}  [{project}]  {info[‘exchanges’]} exchanges")

            # Session title (AI-generated)
            lines.append(f"  Title:  {info[‘title’]}")

            # First thing the user actually said
            if info["first_prompt"]:
                prompt = info["first_prompt"]
                if len(prompt) == 120:
                    prompt += "…"
                lines.append(f"  First:  {prompt}")

            # Time span
            if info["first_ts"] and info["last_ts"]:
                def fmt_ts(ts):
                    try:
                        dt_utc = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        return dt_utc.astimezone().strftime("%Y-%m-%d %H:%M")
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
        script = str(Path(__file__).parent / "ai_search_app.py")
        import subprocess

        subprocess.Popen(
            ["python", script],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def plugin_unloaded():
    """Clean up when plugin unloads."""
    v = _ai_view()
    if v:
        try:
            vid = str(v.id())
            row_count = v.rowcol(v.size())[0] + 1
            last_line = _view_state.get(vid, {}).get("last_line", 0)
            if last_line < row_count:
                start_point = v.text_point(last_line, 0)
                new_text = v.substr(sublime.Region(start_point, v.size()))
                if new_text.strip():
                    _append_log(new_text)
                    _diagnostic_log(f"UNLOAD_FLUSH: saved {len(new_text)} chars")
        except Exception:
            pass
    _save_state()
    _diagnostic_log("plugin_unloaded")
