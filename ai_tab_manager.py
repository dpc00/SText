"""ai_tab_manager.py " robust logging for Ai sessions in Sublime Text.

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

import calendar
import datetime
import json
import math
import os
import subprocess
import threading
import time
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


def _append_log(text: str, session_tag: str = "") -> None:
    """Append text to today's conversation log with timestamp and optional session tag."""
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        log_file = os.path.join(_LOG_DIR, f"ai_{datetime.date.today().isoformat()}.log")
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        header = f"\n[{ts}{(' ' + session_tag) if session_tag else ''}]\n"
        with open(log_file, "a", encoding="utf-8", newline="") as f:
            f.write(header + _clean_text(text))
    except OSError as e:
        _diagnostic_log(f"WRITE_ERROR: Failed to write to log: {e}")


def _claude_views():
    """Return all Terminus views with a live claude process, with session info."""
    results = []
    try:
        from Terminus.terminus.terminal import Terminal  # type: ignore
    except ImportError:
        return results
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
            if "claude" in argv0.lower() and alive:
                pid = t.process.pid
                name = v.name() or f"view{v.id()}"
                results.append((v, pid, name))
    return results


def _is_ai_view(v):
    """Return True if v is a tracked claude Terminus view."""
    if str(v.id()) in _view_state:
        return True
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


def _screenshot_via_mcp(filepath: str) -> bool:
    """Capture the ST window via screenshot-mcp (works even when window is obscured).

    Spawns screenshot-mcp as a subprocess, exchanges MCP JSON-RPC over stdio,
    and calls screenshot_window with window_title="Sublime Text".
    """
    import base64
    import json

    try:
        bun_exe = str(Path.home() / ".bun" / "bin" / "bun.exe")
        if not os.path.exists(bun_exe):
            bun_exe = "bun"

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

        mcp_script = str(Path.home() / "node_modules" / "screenshot-mcp" / "src" / "index.ts")
        proc = subprocess.Popen(
            [bun_exe, "run", mcp_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=startupinfo,
        )

        def send(obj):
            proc.stdin.write((json.dumps(obj) + "\n").encode())
            proc.stdin.flush()

        def recv(expected_id):
            for _ in range(20):
                line = proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == expected_id:
                    return msg
            return {}

        send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ai_tab_manager", "version": "1.0"},
            },
        })
        recv(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "list_windows", "arguments": {}},
        })
        list_resp = recv(2)
        window_id = None
        list_content = list_resp.get("result", {}).get("content", [])
        if list_content:
            windows = json.loads(list_content[0].get("text", "[]"))
            match = next(
                (w for w in windows if "sublime" in w.get("app", "").lower()),
                None,
            )
            if match:
                window_id = match["id"]
        if not window_id:
            _diagnostic_log("SCREENSHOT_MCP_FAIL: no sublime_text window found in list_windows")
            proc.terminate()
            return False

        send({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {
                "name": "screenshot_window",
                "arguments": {"window_id": window_id},
            },
        })
        response = recv(3)
        proc.terminate()

        content = response.get("result", {}).get("content", [])
        for item in content:
            if item.get("type") == "image":
                img_bytes = base64.b64decode(item["data"])
                with open(filepath, "wb") as f:
                    f.write(img_bytes)
                return True
        error_text = next((i.get("text", "") for i in content if i.get("type") == "text"), "")
        _diagnostic_log(f"SCREENSHOT_MCP_FAIL: {error_text or 'no image in response'}")
        return False
    except Exception as e:
        _diagnostic_log(f"SCREENSHOT_MCP_ERROR: {e}")
        return False


def _screenshot_hash(filepath: str) -> str:
    import hashlib
    with open(filepath, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _perceptual_hash(filepath: str, hash_size: int = 8) -> int:
    """
    Compute dHash via Windows GDI+ (no PIL needed).
    Resizes image to (hash_size+1) x hash_size, converts to grayscale, compares
    adjacent pixels left-to-right per row to produce a 64-bit integer hash.
    Returns 0 on any failure (caller falls back to MD5).
    """
    import ctypes

    class _StartupInput(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("callback", ctypes.c_void_p),
                    ("suppress_bg", ctypes.c_bool), ("suppress_ec", ctypes.c_bool)]

    class _BitmapData(ctypes.Structure):
        _fields_ = [("Width", ctypes.c_uint), ("Height", ctypes.c_uint),
                    ("Stride", ctypes.c_int), ("PixelFormat", ctypes.c_int),
                    ("Scan0", ctypes.c_void_p), ("Reserved", ctypes.c_void_p)]

    try:
        gdi = ctypes.WinDLL("gdiplus", use_last_error=True)
        token = ctypes.c_ulong(0)
        si = _StartupInput(1, None, False, False)
        if gdi.GdiplusStartup(ctypes.byref(token), ctypes.byref(si), None) != 0:
            return 0

        img = ctypes.c_void_p(0)
        try:
            path_w = ctypes.create_unicode_buffer(str(filepath))
            if gdi.GdipLoadImageFromFile(path_w, ctypes.byref(img)) != 0:
                return 0

            tw, th = hash_size + 1, hash_size
            thumb = ctypes.c_void_p(0)
            if gdi.GdipGetImageThumbnailImage(img, tw, th, ctypes.byref(thumb), None, None) != 0:
                return 0

            bd = _BitmapData()
            rect = (ctypes.c_int * 4)(0, 0, tw, th)
            PIXEL_FORMAT_32BPP_ARGB = 0x0026200A
            if gdi.GdipBitmapLockBits(thumb, rect, 1, PIXEL_FORMAT_32BPP_ARGB, ctypes.byref(bd)) != 0:
                gdi.GdipDisposeImage(thumb)
                return 0

            try:
                buf = (ctypes.c_uint8 * (tw * th * 4)).from_address(bd.Scan0)
                gray = [
                    int(0.299 * buf[i * 4 + 2] + 0.587 * buf[i * 4 + 1] + 0.114 * buf[i * 4])
                    for i in range(tw * th)
                ]
            finally:
                gdi.GdipBitmapUnlockBits(thumb, ctypes.byref(bd))
                gdi.GdipDisposeImage(thumb)

            h = 0
            for row in range(th):
                for col in range(hash_size):
                    if gray[row * tw + col] > gray[row * tw + col + 1]:
                        h |= 1 << (row * hash_size + col)
            return h
        finally:
            if img:
                gdi.GdipDisposeImage(img)
            gdi.GdiplusShutdown(token)
    except Exception:
        return 0


def _images_similar(fp1: str, fp2: str, threshold: int = 8) -> bool:
    """True if two screenshots are perceptually similar (Hamming distance <= threshold)."""
    h1 = _perceptual_hash(fp1)
    h2 = _perceptual_hash(fp2)
    if h1 and h2:
        return bin(h1 ^ h2).count("1") <= threshold
    return _screenshot_hash(fp1) == _screenshot_hash(fp2)


def _take_screenshot(view_id: str) -> None:
    """Capture ST window screenshot via screenshot-mcp, skipping near-duplicates."""
    import threading

    try:
        os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(_SCREENSHOT_DIR, f"ai_{timestamp}.png")
        if not _screenshot_via_mcp(filepath):
            _diagnostic_log("SCREENSHOT: Failed to capture ST window via screenshot-mcp")
            return

        def _dedup(fp):
            try:
                existing = sorted(
                    f for f in os.listdir(_SCREENSHOT_DIR)
                    if f.endswith(".png") and f != os.path.basename(fp)
                )
                if existing:
                    prev = os.path.join(_SCREENSHOT_DIR, existing[-1])
                    if _images_similar(fp, prev):
                        os.remove(fp)
                        return
                _diagnostic_log(f"SCREENSHOT: Captured ST window to {fp}")
            except Exception as e:
                _diagnostic_log(f"SCREENSHOT_DEDUP_ERROR: {e}")

        threading.Thread(target=_dedup, args=(filepath,), daemon=True).start()

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

    # Detect if buffer shrunk (trim/reload) — adjust last_line to match the new top position
    # so we don't re-log already-captured content.
    if current_row_count < last_row_count:
        lost_lines = last_row_count - current_row_count
        if lost_lines > 5:
            old_last_line = state.get("last_line", 0)
            new_last_line = max(0, old_last_line - lost_lines)
            _diagnostic_log(
                f"TRIM: {lost_lines} lines removed from buffer top "
                f"(last_line {old_last_line} → {new_last_line})"
            )
            state["anomalies"] = state.get("anomalies", 0) + 1
            state["last_line"] = new_last_line

    # Detect long pauses " debounced to once per 5 minutes to avoid log flood
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

    claude_views = _claude_views()
    multi = len(claude_views) > 1

    for v, pid, name in claude_views:
        vid = str(v.id())
        row_count = v.rowcol(v.size())[0] + 1
        session_tag = f"pid={pid} {name}" if multi else ""

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

        if last_line > row_count:
            _diagnostic_log(f"RESET: last_line={last_line} > row_count={row_count}, resetting to 0")
            state["last_line"] = 0
            last_line = 0

        _detect_anomalies(vid, row_count, last_row_count)
        state["last_row_count"] = row_count

        if row_count > last_line:
            try:
                start_point = v.text_point(last_line, 0) if last_line < row_count else v.size()
                end_point = v.size()
                if start_point < end_point:
                    new_text = v.substr(sublime.Region(start_point, end_point))
                    _append_log(new_text, session_tag)
                    state["last_line"] = row_count
                    state["last_output_time"] = __import__("time").time()
                    _save_state()
            except Exception as e:
                _diagnostic_log(f"LOG_ERROR: Failed to log lines: {e}")

    # Periodic screenshot — unconditional, not tied to Ai view
    import time

    current_time = time.time()
    _SS_KEY = "__screenshot__"
    if _SS_KEY not in _last_screenshot_time:
        _last_screenshot_time[_SS_KEY] = current_time
    elif current_time - _last_screenshot_time[_SS_KEY] > _SCREENSHOT_INTERVAL:
        _last_screenshot_time[_SS_KEY] = current_time
        threading.Thread(target=_take_screenshot, args=(_SS_KEY,), daemon=True).start()

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

            _screenshot_via_mcp(filepath)

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

            # Header: date + project + exchange count
            dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
            lines.append(f"{dt}  [{project}]  {info['exchanges']} exchanges")

            # Session title (AI-generated)
            lines.append(f"  Title:  {info['title']}")

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
        import socket, subprocess, webbrowser
        url = "http://127.0.0.1:5758"

        def _port_free(p):
            with socket.socket() as s:
                try: s.connect(("127.0.0.1", p)); return False
                except OSError: return True

        if _port_free(5758):
            script = str(Path(__file__).parent / "ai_search_app.py")
            subprocess.Popen(
                ["python", script],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            webbrowser.open(url)


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
                killed.append(name)  # server died before responding — still counts
        msg = f"Quit: {', '.join(killed)}" if killed else "No Flask apps were running"
        sublime.status_message(msg)


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
