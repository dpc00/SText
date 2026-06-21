"""ai_logger.py — Logging for Ai sessions in Sublime Text.

Polls the Terminus "Ai" view and tees newly-committed terminal lines to a
daily log file.  Buffer management (trimming) lives in ai_tab_manager.py.

LOGGING STRATEGY
================
Logs ONLY lines that have scrolled into Terminus committed history
(above terminal.offset).  The live screen below the offset is repainted
constantly by escape codes and is never logged directly.  A short content
anchor (last 8 logged lines) keeps the position aligned across scrollback
trims that shift row numbers without changing line content.
"""

import datetime
import json
import os
import re as _re
import subprocess
import threading
import time
from pathlib import Path

import sublime  # type: ignore
import sublime_plugin  # type: ignore


# -- constants ----------------------------------------------------------------

_LOG_DIR = str(Path.home() / ".claude" / "conversation_logs")
_STATE_FILE = str(Path.home() / ".claude" / "ai_logger_state.json")
_SCREENSHOT_DIR = str(Path.home() / ".claude" / "screenshots")
_DIAGNOSTICS_FILE = str(Path.home() / ".claude" / "ai_diagnostics.log")
_CHECK_MS = 500
_SCREENSHOT_INTERVAL = 60
_SCREENSHOT_RETENTION_DAYS = 7
# -- globals ------------------------------------------------------------------

_view_state = {}           # view_id -> {anchor, started_at, last_output_time, ...}
_last_screenshot_time = {} # __screenshot__ -> timestamp
_last_cleanup_time = 0


# -- state persistence --------------------------------------------------------

def _load_state():
    global _view_state
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                _view_state = json.load(f)
    except (OSError, json.JSONDecodeError):
        _view_state = {}


def _save_state():
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_view_state, f, indent=2)
    except OSError as e:
        print(f"ai_logger: ERROR saving state: {e}")


# -- diagnostic log -----------------------------------------------------------

def _diagnostic_log(message: str) -> None:
    try:
        os.makedirs(os.path.dirname(_DIAGNOSTICS_FILE), exist_ok=True)
        with open(_DIAGNOSTICS_FILE, "a", encoding="utf-8") as f:
            timestamp = datetime.datetime.now().isoformat()
            f.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass


# -- screenshot cleanup -------------------------------------------------------

def _cleanup_old_screenshots() -> None:
    try:
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


# -- text cleaning ------------------------------------------------------------

_TRAIL_JUNK = _re.compile(r"[\s─-╿▀-▟]+$")
_STATUS_BAR_GAP = _re.compile(r"\S\s{20,}\S")


def _clean_text(text: str) -> str:
    """Normalize terminal buffer text for clean log output."""
    text = text.replace("\xa0", " ")
    text = (
        text.replace("​", "")
            .replace("‌", "")
            .replace("‍", "")
            .replace("﻿", "")
    )
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        line = _TRAIL_JUNK.sub("", line)
        if len(line) > 100 and _STATUS_BAR_GAP.search(line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


# -- log append ---------------------------------------------------------------

def _append_log(text: str, session_tag: str = "") -> None:
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        log_file = os.path.join(_LOG_DIR, f"ai_{datetime.date.today().isoformat()}.log")
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        header = f"\n[{ts}{(' ' + session_tag) if session_tag else ''}]\n"
        with open(log_file, "a", encoding="utf-8", newline="") as f:
            f.write(header + _clean_text(text))
    except OSError as e:
        _diagnostic_log(f"WRITE_ERROR: Failed to write to log: {e}")


# -- screenshot ---------------------------------------------------------------

def _screenshot_via_mcp(filepath: str) -> bool:
    """Capture the ST window via screenshot-mcp."""
    import base64

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
                "clientInfo": {"name": "ai_logger", "version": "1.0"},
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
            _diagnostic_log("SCREENSHOT_MCP_FAIL: no sublime_text window found")
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
    """Compute dHash via Windows GDI+.  Returns 0 on failure."""
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
    h1 = _perceptual_hash(fp1)
    h2 = _perceptual_hash(fp2)
    if h1 and h2:
        return bin(h1 ^ h2).count("1") <= threshold
    return _screenshot_hash(fp1) == _screenshot_hash(fp2)


def _take_screenshot(view_id: str) -> None:
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


# -- view detection (duplicated from ai_tab_manager.py for independence) ------

def _get_child_process_names() -> dict:
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
    try:
        from Terminus.terminus.terminal import Terminal
        t = Terminal.from_id(v.id())
        return bool(t and t.process and "claude" in (t.process.argv[0] if t.process.argv else "").lower())
    except Exception:
        return False


def _ai_view():
    views = _claude_views()
    return views[0][0] if views else None


# -- core logging logic -------------------------------------------------------

def _flush_view(view: "sublime.View", session_tag: str = "") -> None:
    """Tee newly-finalized terminal lines to today's log, each line exactly once.

    Logs ONLY Terminus committed history — the view rows above ``terminal.offset``,
    i.e. lines that have scrolled out of the live screen and can no longer be
    repainted. The live screen region below the offset (spinner, status bar, input
    box, boot banner) is never logged; re-logging that repainting region is what
    used to cause the massive duplication. A short content anchor (the last few
    logged lines) keeps us aligned across scrollback trimming, which shifts row
    numbers but never the line content.
    """
    vid = str(view.id())
    try:
        from Terminus.terminus.terminal import Terminal  # type: ignore
        term = Terminal.from_id(view.id())
    except Exception:
        term = None
    if term is None:
        return
    try:
        total = view.rowcol(view.size())[0] + 1
        offset = int(getattr(term, "offset", 0) or 0)
        offset = max(0, min(offset, total))
        if offset <= 0:
            return  # nothing has scrolled into committed history yet

        committed = view.substr(sublime.Region(0, view.text_point(offset, 0)))
        lines = committed.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if not lines:
            return

        state = _view_state.setdefault(vid, {})
        anchor = state.get("anchor") or []
        if not anchor:
            if "last_line" in state:
                # Carried over from old row-count logger: skip re-baseline.
                state.pop("last_line", None)
                new_lines = []
            else:
                new_lines = lines  # brand-new view: capture its existing history once
        else:
            k = len(anchor)
            start = None
            for i in range(len(lines) - 1, k - 2, -1):
                if lines[i - k + 1:i + 1] == anchor:
                    start = i + 1
                    break
            if start is None:
                _diagnostic_log("TEE: anchor not found (scrolled past between ticks); re-syncing")
                new_lines = []
            else:
                new_lines = lines[start:]

        if new_lines:
            _append_log("\n".join(new_lines), session_tag)
        state["anchor"] = lines[-8:]
        state["last_output_time"] = time.time()
        _save_state()
    except Exception as e:
        _diagnostic_log(f"FLUSH_ERROR: {e}")


def _tick():
    """Main polling loop: check for new content every _CHECK_MS milliseconds."""
    global _last_cleanup_time

    current_time = time.time()
    if current_time - _last_cleanup_time > 9000:
        _cleanup_old_screenshots()
        _last_cleanup_time = current_time

    claude_views = _claude_views()
    multi = len(claude_views) > 1

    for v, pid, name in claude_views:
        vid = str(v.id())
        session_tag = f"pid={pid} {name}" if multi else ""

        if vid not in _view_state:
            _view_state[vid] = {
                "anchor": [],
                "started_at": datetime.datetime.now().isoformat(),
            }

        _flush_view(v, session_tag)

    _SS_KEY = "__screenshot__"
    if _SS_KEY not in _last_screenshot_time:
        _last_screenshot_time[_SS_KEY] = current_time
    elif current_time - _last_screenshot_time[_SS_KEY] > _SCREENSHOT_INTERVAL:
        _last_screenshot_time[_SS_KEY] = current_time
        threading.Thread(target=_take_screenshot, args=(_SS_KEY,), daemon=True).start()

    sublime.set_timeout(_tick, _CHECK_MS)


# -- commands -----------------------------------------------------------------

class AiCaptureScrollPositionCommand(sublime_plugin.TextCommand):
    """Screenshot ST at the current scroll position."""

    def run(self, edit):
        v = self.view
        if not _is_ai_view(v):
            sublime.error_message("This command only works in the Ai view")
            return

        try:
            visible_region = v.visible_region()
            row, col = v.rowcol(visible_region.begin())

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
            _diagnostic_log(f"SCREENSHOT_ERROR: {e}")
            sublime.error_message(f"Failed to capture screenshot: {e}")


# -- event listener -----------------------------------------------------------

class AiLoggerEventListener(sublime_plugin.EventListener):
    """Flush the Ai buffer when the view or window closes."""

    def on_pre_close(self, view: sublime.View) -> None:
        if _is_ai_view(view):
            _flush_view(view)

    def on_window_command(self, window, command_name, args):
        if command_name in ("close_window", "exit"):
            v = _ai_view()
            if v:
                _flush_view(v)


# -- lifecycle -----------------------------------------------------------------

def plugin_loaded():
    _load_state()
    os.makedirs(_LOG_DIR, exist_ok=True)
    _cleanup_old_screenshots()
    sublime.set_timeout(_tick, _CHECK_MS)
    msg = (
        f"ai_logger: initialized "
        f"(polling every {_CHECK_MS}ms, "
        f"screenshots every {_SCREENSHOT_INTERVAL}s)"
    )
    print(msg)
    _diagnostic_log(msg)


def plugin_unloaded():
    global _watcher_proc
    v = _ai_view()
    if v:
        _flush_view(v)
    _save_state()
    _diagnostic_log("ai_logger: unloaded")
