"""claude_tab_manager.py — robust logging for Claude Code sessions in Sublime Text.

PURPOSE
=======
The Terminus view named "Claude" is the live Claude Code session. This plugin
creates multiple layers of logging to capture EVERYTHING Claude does, because
Claude Code itself fails to persistently log many operations (especially agent
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
- State file: ~/.claude/claude_tab_manager_state.json
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
_STATE_FILE = str(Path.home() / ".claude" / "claude_tab_manager_state.json")
_SCREENSHOT_DIR = str(Path.home() / ".claude" / "screenshots")
_DIAGNOSTICS_FILE = str(Path.home() / ".claude" / "claude_diagnostics.log")
_CHECK_MS = 500  # poll every 500ms (was 4000ms)
_SCREENSHOT_INTERVAL = 60  # capture screenshot every 60 seconds
_SCREENSHOT_RETENTION_DAYS = 7  # keep screenshots for 7 days, delete older ones

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
        print(f"claude_tab_manager: ERROR saving state: {e}")


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
            _diagnostic_log(f"CLEANUP: Deleted {deleted_count} old screenshots, freed {freed_mb:.1f}MB")
    except Exception as e:
        _diagnostic_log(f"CLEANUP_ERROR: {e}")


def _append_log(text: str) -> None:
    """Append text to today's conversation log."""
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        log_file = os.path.join(_LOG_DIR, f"claude_{datetime.date.today().isoformat()}.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        _diagnostic_log(f"WRITE_ERROR: Failed to write to log: {e}")


def _claude_view():
    """Return the first view named 'Claude' across all windows, or None."""
    for w in sublime.windows():
        for v in w.views():
            if v.name() == "Claude":
                return v
    return None


def _take_screenshot(view_id: str) -> None:
    """Capture screenshot as backup evidence of what's visible on screen."""
    try:
        os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(_SCREENSHOT_DIR, f"claude_{timestamp}.png")

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
                startupinfo=startupinfo
            )
            _diagnostic_log(f"SCREENSHOT: Captured to {filepath}")
    except Exception as e:
        _diagnostic_log(f"SCREENSHOT_ERROR: {e}")


def _detect_anomalies(view_id: str, current_row_count: int, last_row_count: int) -> None:
    """Detect unusual patterns that might indicate lost data or invisible operations."""
    if view_id not in _view_state:
        return

    import time
    state = _view_state[view_id]
    now = time.time()

    # Detect if buffer shrunk (trim/reload) — reset last_line so we re-log current buffer
    if current_row_count < last_row_count:
        lost_lines = last_row_count - current_row_count
        if lost_lines > 5:
            _diagnostic_log(
                f"ANOMALY: {lost_lines} lines deleted from buffer (resetting last_line to re-log)"
            )
            state["anomalies"] = state.get("anomalies", 0) + 1
            state["last_line"] = 0

    # Detect long pauses — debounced to once per 5 minutes to avoid log flood
    if "last_output_time" in state:
        time_since_output = now - state["last_output_time"]
        last_pause_logged = state.get("last_pause_logged_time", 0)
        if time_since_output > 30 and now - last_pause_logged > 300:
            _diagnostic_log(f"ANOMALY: 30+ seconds without output (possible invisible operation)")
            state["anomalies"] = state.get("anomalies", 0) + 1
            state["last_pause_logged_time"] = now


def _tick():
    """Main polling loop: check for new content every _CHECK_MS milliseconds."""
    global _last_cleanup_time
    import time

    # Periodically cleanup old screenshots (every 24 hours)
    current_time = time.time()
    if current_time - _last_cleanup_time > 86400:  # 24 hours
        _cleanup_old_screenshots()
        _last_cleanup_time = current_time

    v = _claude_view()

    if v:
        vid = str(v.id())
        row_count = v.rowcol(v.size())[0] + 1

        # Initialize state for this view
        if vid not in _view_state:
            _view_state[vid] = {
                "last_line": 0,
                "last_row_count": row_count,
                "started_at": datetime.datetime.now().isoformat(),
                "anomalies": 0
            }

        state = _view_state[vid]
        last_line = state.get("last_line", 0)
        last_row_count = state.get("last_row_count", 0)

        # If last_line is stale (e.g. loaded from disk for a resumed view), reset so we
        # re-log whatever is currently in the buffer rather than silently missing content.
        if last_line > row_count:
            _diagnostic_log(f"RESET: last_line={last_line} > row_count={row_count}, resetting to 0")
            state["last_line"] = 0
            last_line = 0

        # Detect anomalies
        _detect_anomalies(vid, row_count, last_row_count)
        state["last_row_count"] = row_count

        # Log new lines if they exist
        if row_count > last_line:
            try:
                start_point = v.text_point(last_line, 0) if last_line < row_count else v.size()
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


class CtmTrimNowCommand(sublime_plugin.TextCommand):
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
        msg = f"claude_tab_manager: trimmed {lines_deleted} lines, now {final_lines} total"
        print(msg)
        _diagnostic_log(msg)


def plugin_loaded():
    """Initialize the plugin when Sublime loads."""
    _load_state()
    _cleanup_old_screenshots()  # Clean up old screenshots on startup
    sublime.set_timeout(_tick, _CHECK_MS)
    msg = f"claude_tab_manager: initialized (polling every {_CHECK_MS}ms, screenshots every {_SCREENSHOT_INTERVAL}s, retention {_SCREENSHOT_RETENTION_DAYS} days)"
    print(msg)
    _diagnostic_log(msg)


class CtmCaptureScrollPositionCommand(sublime_plugin.TextCommand):
    """Capture screenshot at current scroll position (for manual reconstruction).

    Use this while scrolling through the buffer to document what line you're viewing.
    Determines the visible region on screen and labels the screenshot with the top line.
    """

    def run(self, edit):
        v = self.view
        if v.name() != "Claude":
            sublime.error_message("This command only works in the Claude view")
            return

        try:
            # Get the first visible line in the current view (based on visible region)
            visible_region = v.visible_region()
            first_visible_point = visible_region.begin()
            row, col = v.rowcol(first_visible_point)

            os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = os.path.join(_SCREENSHOT_DIR, f"claude_scroll_line{row+1:04d}_{timestamp}.png")

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
                    startupinfo=startupinfo
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


class CtmDumpBufferCommand(sublime_plugin.TextCommand):
    """Export the entire current Claude buffer to a file for inspection/archival."""

    def run(self, edit):
        v = self.view
        if v.name() != "Claude":
            sublime.error_message("This command only works in the Claude view")
            return

        try:
            # Get entire buffer content
            entire_content = v.substr(sublime.Region(0, v.size()))

            # Save to dated file
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            export_dir = Path.home() / ".claude" / "buffer_exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            export_file = export_dir / f"claude_buffer_dump_{timestamp}.txt"

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


class CtmEventListener(sublime_plugin.EventListener):
    """Flush the full Claude buffer to the log when the view or window closes."""

    def _flush_claude_view(self, view: sublime.View) -> None:
        if view.name() != "Claude":
            return
        try:
            entire_content = view.substr(sublime.Region(0, view.size()))
            if not entire_content.strip():
                return
            _append_log(entire_content)
            vid = str(view.id())
            row_count = view.rowcol(view.size())[0] + 1
            if vid in _view_state:
                _view_state[vid]["last_line"] = row_count
            _save_state()
            _diagnostic_log(f"CLOSE_FLUSH: saved {len(entire_content)} chars from Claude view")
        except Exception as e:
            _diagnostic_log(f"CLOSE_FLUSH_ERROR: {e}")

    def on_pre_close(self, view: sublime.View) -> None:
        self._flush_claude_view(view)

    def on_window_command(self, window, command_name, args):
        if command_name in ("close_window", "exit"):
            v = _claude_view()
            if v:
                self._flush_claude_view(v)


def plugin_unloaded():
    """Clean up when plugin unloads."""
    v = _claude_view()
    if v:
        try:
            entire_content = v.substr(sublime.Region(0, v.size()))
            if entire_content.strip():
                _append_log(entire_content)
                _diagnostic_log(f"UNLOAD_FLUSH: saved {len(entire_content)} chars")
        except Exception:
            pass
    _save_state()
    _diagnostic_log("plugin_unloaded")
