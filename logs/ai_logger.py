"""ai_logger.py -- Log Claude Code sessions by tailing the JSONL transcript.

Replaces the old Terminus-scraping approach (wrong timestamps, duplication from
repaints, content lost in live area) with direct JSONL tailing.  Claude Code
writes an append-only .jsonl to ~/.claude/projects/{slug}/{session}.jsonl.
Every record has an accurate UTC timestamp; nothing is lost when Claude closes.

Architecture:
    Claude CLI  ->  ~/.claude/projects/{slug}/{session}.jsonl  (append-only)
                              |
    _tick() finds newest JSONL, reads new bytes from saved offset
                              |
    _record_to_lines() formats each record type  ->  daily log file
"""

import datetime
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import sublime  # type: ignore
import sublime_plugin  # type: ignore


# -- constants ----------------------------------------------------------------

_LOG_DIR = str(Path.home() / "data" / "logs" / "ai")
_STATE_FILE = str(Path.home() / "data" / "state" / "ai_logger_state.json")
_SCREENSHOT_DIR = str(Path.home() / "data" / "screenshots")
_DIAGNOSTICS_FILE = str(Path.home() / "data" / "logs" / "ai" / "ai_diagnostics.log")
_PROJECTS_DIR = str(Path.home() / ".claude" / "projects")
_CHECK_MS = 500
_SCREENSHOT_INTERVAL = 60
_tick_active = False  # guard: only one _tick loop may be scheduled at a time
_SCREENSHOT_RETENTION_DAYS = 7
_PANIC_THRESHOLD = 1  # output_tokens — trigger on every assistant response
_AUTO_PANIC = False  # disabled: intercepts agent SDK responses it shouldn't
_PANIC_RESPONSE_VIEW = "Panic: Response"
_AI_VIEW_SETTING = "ai_logger"

# -- globals ------------------------------------------------------------------

_jsonl_state = {}  # jsonl_path -> {"offset": int}
_id2name = {}  # tool_use id -> tool name
_last_screenshot_time = {}
_last_cleanup_time = 0
_last_record_ts = ""  # ISO timestamp of last record written to log
_current_jsonl = None  # path currently being flushed
_seen_uuids = {}  # uuid -> order (insertion counter, for trimming)
_seen_uuid_counter = 0  # monotonic insertion counter
_SEEN_UUIDS_MAX = 2000  # max UUIDs kept across reloads
_new_turn = (
    True  # True after a user message — first assistant record replaces, rest append
)


# -- state persistence --------------------------------------------------------


def _load_state():
    global _jsonl_state, _last_record_ts, _current_jsonl, _seen_uuids, _seen_uuid_counter
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _last_record_ts = data.pop("__last_record_ts__", "")
            _current_jsonl = data.pop("__current_jsonl__", None)
            saved_uuids = data.pop("__seen_uuids__", [])
            _seen_uuids = {u: i for i, u in enumerate(saved_uuids)}
            _seen_uuid_counter = len(_seen_uuids)
            _jsonl_state = data
    except (OSError, json.JSONDecodeError):
        _jsonl_state = {}


def _save_state():
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        data = dict(_jsonl_state)
        data["__last_record_ts__"] = _last_record_ts
        data["__current_jsonl__"] = _current_jsonl
        # Save UUIDs sorted by insertion order (most recent last), trimmed to max
        sorted_uuids = sorted(_seen_uuids, key=lambda u: _seen_uuids[u])
        data["__seen_uuids__"] = sorted_uuids[-_SEEN_UUIDS_MAX:]
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        print(f"ai_logger: ERROR saving state: {e}")


# -- diagnostic log -----------------------------------------------------------


def _diagnostic_log(message: str) -> None:
    try:
        os.makedirs(os.path.dirname(_DIAGNOSTICS_FILE), exist_ok=True)
        with open(_DIAGNOSTICS_FILE, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().isoformat()
            f.write(f"[{ts}] {message}\n")
    except OSError:
        pass


# -- screenshot cleanup -------------------------------------------------------


def _cleanup_old_screenshots() -> None:
    try:
        sd = Path(_SCREENSHOT_DIR)
        if not sd.exists():
            return
        cutoff = time.time() - (_SCREENSHOT_RETENTION_DAYS * 86400)
        deleted, freed = 0, 0
        for f in sd.glob("*.png"):
            if os.path.getmtime(f) < cutoff:
                try:
                    freed += os.path.getsize(f)
                    os.remove(f)
                    deleted += 1
                except OSError:
                    pass
        if deleted:
            _diagnostic_log(
                f"CLEANUP: {deleted} screenshots, {freed/1048576:.1f}MB freed"
            )
    except Exception as e:
        _diagnostic_log(f"CLEANUP_ERROR: {e}")


# -- screenshot ---------------------------------------------------------------


def _screenshot_via_mcp(filepath: str) -> bool:
    import base64

    try:
        bun_exe = str(Path.home() / ".bun" / "bin" / "bun.exe")
        if not os.path.exists(bun_exe):
            bun_exe = "bun"
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        mcp_script = str(
            Path.home() / "node_modules" / "screenshot-mcp" / "src" / "index.ts"
        )
        proc = subprocess.Popen(
            [bun_exe, "run", mcp_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=si,
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

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "ai_logger", "version": "2.0"},
                },
            }
        )
        recv(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_windows", "arguments": {}},
            }
        )
        list_resp = recv(2)
        window_id = None
        for item in list_resp.get("result", {}).get("content", []):
            try:
                windows = json.loads(item.get("text", "[]"))
                match = next(
                    (w for w in windows if "sublime" in w.get("app", "").lower()), None
                )
                if match:
                    window_id = match["id"]
                    break
            except Exception:
                pass
        if not window_id:
            _diagnostic_log("SCREENSHOT: no sublime_text window found")
            proc.terminate()
            return False
        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "screenshot_window",
                    "arguments": {"window_id": window_id},
                },
            }
        )
        response = recv(3)
        proc.terminate()
        for item in response.get("result", {}).get("content", []):
            if item.get("type") == "image":
                with open(filepath, "wb") as f:
                    f.write(base64.b64decode(item["data"]))
                return True
        return False
    except Exception as e:
        _diagnostic_log(f"SCREENSHOT_ERROR: {e}")
        return False


def _screenshot_hash(filepath: str) -> str:
    import hashlib

    with open(filepath, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _perceptual_hash(filepath: str, hash_size: int = 8) -> int:
    import ctypes

    class _StartupInput(ctypes.Structure):
        _fields_ = [
            ("version", ctypes.c_uint32),
            ("callback", ctypes.c_void_p),
            ("suppress_bg", ctypes.c_bool),
            ("suppress_ec", ctypes.c_bool),
        ]

    class _BitmapData(ctypes.Structure):
        _fields_ = [
            ("Width", ctypes.c_uint),
            ("Height", ctypes.c_uint),
            ("Stride", ctypes.c_int),
            ("PixelFormat", ctypes.c_int),
            ("Scan0", ctypes.c_void_p),
            ("Reserved", ctypes.c_void_p),
        ]

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
            if (
                gdi.GdipGetImageThumbnailImage(
                    img, tw, th, ctypes.byref(thumb), None, None
                )
                != 0
            ):
                return 0
            bd = _BitmapData()
            rect = (ctypes.c_int * 4)(0, 0, tw, th)
            if (
                gdi.GdipBitmapLockBits(thumb, rect, 1, 0x0026200A, ctypes.byref(bd))
                != 0
            ):
                gdi.GdipDisposeImage(thumb)
                return 0
            try:
                buf = (ctypes.c_uint8 * (tw * th * 4)).from_address(bd.Scan0)
                gray = [
                    int(
                        0.299 * buf[i * 4 + 2]
                        + 0.587 * buf[i * 4 + 1]
                        + 0.114 * buf[i * 4]
                    )
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
    h1, h2 = _perceptual_hash(fp1), _perceptual_hash(fp2)
    if h1 and h2:
        return bin(h1 ^ h2).count("1") <= threshold
    return _screenshot_hash(fp1) == _screenshot_hash(fp2)


def _take_screenshot(key: str) -> None:
    try:
        os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = os.path.join(_SCREENSHOT_DIR, f"ai_{ts}.png")
        if not _screenshot_via_mcp(fp):
            return

        def _dedup(fp):
            try:
                existing = sorted(
                    f
                    for f in os.listdir(_SCREENSHOT_DIR)
                    if f.endswith(".png") and f != os.path.basename(fp)
                )
                if existing:
                    prev = os.path.join(_SCREENSHOT_DIR, existing[-1])
                    if _images_similar(fp, prev):
                        os.remove(fp)
                        return
                _diagnostic_log(f"SCREENSHOT: {fp}")
            except Exception as e:
                _diagnostic_log(f"SCREENSHOT_DEDUP_ERROR: {e}")

        threading.Thread(target=_dedup, args=(fp,), daemon=True).start()
    except Exception as e:
        _diagnostic_log(f"SCREENSHOT_ERROR: {e}")


# -- JSONL discovery ----------------------------------------------------------

_SWITCH_TIMEOUT = 10  # seconds current JSONL must be idle before switching


def _find_active_transcript():
    """Return path to the active .jsonl, sticky until current goes cold."""
    projects = Path(_PROJECTS_DIR)
    if not projects.exists():
        return None
    best, best_mtime = None, 0
    try:
        for slug_dir in projects.iterdir():
            if not slug_dir.is_dir():
                continue
            for jf in slug_dir.glob("*.jsonl"):
                try:
                    mt = jf.stat().st_mtime
                    if mt > best_mtime:
                        best_mtime, best = mt, str(jf)
                except OSError:
                    pass
    except OSError:
        pass
    if not best:
        return _current_jsonl
    # Stick with the current transcript until it goes cold, to avoid oscillating
    # between the old and new JSONL files during a /cd switch.
    if _current_jsonl and _current_jsonl != best:
        try:
            current_mtime = Path(_current_jsonl).stat().st_mtime
            if time.time() - current_mtime < _SWITCH_TIMEOUT:
                return _current_jsonl
        except OSError:
            pass  # current file gone — switch immediately
    return best


# -- JSONL parsing ------------------------------------------------------------


def _fmt_ts(iso_ts):
    """ISO UTC timestamp -> local HH:MM:SS."""
    try:
        dt = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M:%S")
    except Exception:
        return datetime.datetime.now().strftime("%H:%M:%S")


def _record_local_date(iso_ts):
    """ISO UTC timestamp -> local YYYY-MM-DD (for choosing the daily log file).

    Files by the record's own date, not wall-clock now, so a record processed
    after midnight but timestamped before it lands in the correct day's file.
    Falls back to today on a bad timestamp.
    """
    try:
        dt = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone().date().isoformat()
    except Exception:
        return datetime.date.today().isoformat()


def _flatten_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "\n".join(parts)
    return ""


def _fmt_tool(name, inp):
    """Extract the key field from tool input for readable one-line display."""
    if name == "Bash":
        cmd = (inp.get("command") or "").strip().replace("\n", "; ")
        return cmd[:120] + ("…" if len(cmd) > 120 else "")
    if name == "Read":
        return inp.get("file_path", "")
    if name in ("Edit", "Write"):
        return inp.get("file_path", "")
    if name == "Glob":
        pat = inp.get("pattern", "")
        path = inp.get("path", "")
        return pat + (f" in {path}" if path else "")
    if name == "Grep":
        pat = inp.get("pattern", "")
        path = inp.get("path", "")
        return pat + (f" in {path}" if path else "")
    if name == "WebSearch":
        return inp.get("query", "")
    if name == "WebFetch":
        return inp.get("url", "")
    if name == "Agent":
        desc = inp.get("description") or inp.get("prompt") or ""
        return desc[:80] + ("…" if len(desc) > 80 else "")
    if name == "Skill":
        return inp.get("skill", "")
    # MCP tools: drop prefix, show first meaningful value
    short = name.split("__")[-1] if "__" in name else name
    for key in ("code", "key", "pattern", "command", "text", "path", "url", "query"):
        val = inp.get(key)
        if val and isinstance(val, str):
            val = val[:60].replace("\n", "; ")
            return f"{short}: {val}"
    return short


def _record_to_lines(record, id2name):
    """Convert one JSONL record to log lines. Returns [] to skip."""
    if record.get("isMeta") or record.get("isSidechain"):
        return []
    rtype = record.get("type")
    ts = _fmt_ts(record.get("timestamp", ""))
    out = []

    if rtype == "assistant":
        msg = record.get("message") or {}
        for b in msg.get("content", []) or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "thinking":
                continue
            if bt == "text":
                text = (b.get("text") or "").strip()
                if text:
                    out.append(f"\n[{ts}] Claude:\n{text}")
            elif bt == "tool_use":
                name = b.get("name", "?")
                id2name[b.get("id", "")] = name
                inp = b.get("input") or {}
                detail = _fmt_tool(name, inp)
                out.append(
                    f"  [{ts}] -> {name}: {detail}" if detail else f"  [{ts}] -> {name}"
                )

    elif rtype == "user":
        msg = record.get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            text = c.strip()
            if text:
                out.append(f"\n[{ts}] You: {text}")
        elif isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    tool_name = id2name.get(b.get("tool_use_id", ""), "?")
                    content = _flatten_text(b.get("content") or "")
                    preview = content[:300].replace("\n", " | ")
                    if len(content) > 300:
                        preview += f" ... (+{len(content)-300} chars)"
                    mark = "x" if b.get("is_error") else "ok"
                    out.append(f"  [{ts}] {mark} {tool_name}: {preview}")
                elif b.get("type") == "text":
                    text = (b.get("text") or "").strip()
                    if text:
                        out.append(f"\n[{ts}] You: {text}")

    elif rtype == "system":
        st = record.get("subtype")
        if st == "turn_duration":
            ms = record.get("durationMs", 0)
            out.append(f"  [{ts}] {ms/1000:.1f}s")
        elif st == "api_error":
            err = (record.get("error") or {}).get("message") or "API error"
            out.append(f"  [{ts}] API error: {err}")
        elif st == "compact_boundary":
            out.append(f"\n[{ts}] --- context compacted ---\n")
        elif st == "local_command":
            cmd = (record.get("content") or "").strip()
            if cmd:
                out.append(f"  [{ts}] [cmd] {cmd}")

    return out


# -- log append ---------------------------------------------------------------


def _append_log(date_str, text):
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        log_file = os.path.join(_LOG_DIR, f"ai_{date_str}.log")
        with open(log_file, "a", encoding="utf-8", newline="") as f:
            f.write(text + "\n")
    except OSError as e:
        _diagnostic_log(f"WRITE_ERROR: {e}")


# -- auto-panic ---------------------------------------------------------------


def _panic_is_open():
    for w in sublime.windows():
        for v in w.views():
            if v.name() == _PANIC_RESPONSE_VIEW:
                return True
    return False


def _tool_summary(name, inp):
    if not inp:
        return name
    if "command" in inp:
        return "bash: " + (inp["command"] or "").strip().replace("\n", "; ")
    if "code" in inp:
        return name + ": " + (inp["code"] or "")[:60].replace("\n", " ")
    for key in ("file_path", "path", "pattern"):
        if key in inp:
            return name + ": " + str(inp[key])[:80]
    keys = list(inp.keys())[:1]
    if keys:
        return name + ": " + str(inp[keys[0]])[:60]
    return name


def _format_panic_response(record):
    rtype = record.get("type")
    if rtype == "gemini":
        text = record.get("content") or ""
        parts = []
        if text.strip():
            parts.append(text.strip())
        tool_calls = record.get("toolCalls") or []
        for tc in tool_calls:
            parts.append(
                "  ● " + _tool_summary(tc.get("name", "?"), tc.get("args") or {})
            )
        return "\n\n".join(parts) if parts else ""

    msg = record.get("message") or {}
    content = msg.get("content") or []
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            text = (b.get("text") or "").strip()
            if text:
                parts.append(text)
        elif b.get("type") == "tool_use":
            parts.append(
                "  ● " + _tool_summary(b.get("name", "?"), b.get("input") or {})
            )
    return "\n\n".join(parts) if parts else ""


def _check_auto_panic(record):
    if not _AUTO_PANIC and not _panic_is_open():
        return
    global _new_turn
    rtype = record.get("type")
    # Track turn boundaries
    if rtype == "user":
        msg = record.get("message") or {}
        c = msg.get("content")
        if not c:
            c = record.get("content")
        # Only a real user text message (not tool results) starts a new turn
        if isinstance(c, str) and c.strip():
            _new_turn = True
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    if b.get("type") == "text" or "text" in b:
                        _new_turn = True
                        break
                elif isinstance(b, str) and b.strip():
                    _new_turn = True
                    break
    if rtype not in ("assistant", "gemini"):
        return
    output_tokens = 0
    if rtype == "assistant":
        msg = record.get("message") or {}
        output_tokens = (msg.get("usage") or {}).get("output_tokens", 0)
    elif rtype == "gemini":
        tokens = record.get("tokens") or {}
        output_tokens = tokens.get("output", 0)

    if output_tokens < _PANIC_THRESHOLD:
        return
    text = _format_panic_response(record)
    replace = _new_turn
    _new_turn = False

    def _open(t=text, r=replace):
        w = sublime.active_window()
        if not w:
            return
        if _panic_is_open():
            if r:
                w.run_command("panic_refresh", {"response_text": t})
            else:
                w.run_command("panic_append", {"text": t})
        else:
            w.run_command("panic_open", {"response_text": t})

    sublime.set_timeout(_open, 100)


# -- core flush ---------------------------------------------------------------


def _check_auto_panic_safe(record):
    sublime.set_timeout(lambda: _check_auto_panic(record), 0)


def _flush_jsonl(path):
    """Read any new records from path since last offset and log them."""
    global _last_record_ts, _current_jsonl, _seen_uuids, _seen_uuid_counter
    
    try:
        if not os.path.exists(path):
            return
        size = os.path.getsize(path)
        mtime = os.path.getmtime(path)
    except OSError:
        return

    # Initialize state if not present (setting offset to current file size on first-time discovery)
    is_new = path not in _jsonl_state
    state = _jsonl_state.setdefault(path, {"offset": size, "mtime": mtime})
    if is_new:
        # Save state right away so we don't forget we've seen it
        _save_state()
        return

    if path != _current_jsonl:
        _current_jsonl = path

    # If the file hasn't grown and mtime hasn't changed, skip entirely!
    if size <= state.get("offset", 0) and mtime <= state.get("mtime", 0):
        return

    # Handle file truncation/reset
    if size < state.get("offset", 0):
        state["offset"] = 0

    if size == state.get("offset", 0):
        state["mtime"] = mtime
        return

    try:
        with open(path, "rb") as f:
            f.seek(state["offset"])
            chunk = f.read()
            state["offset"] += len(chunk)
            state["mtime"] = mtime
        if not chunk:
            return

        buf = chunk.decode("utf-8", errors="replace")
        
        # Check if the buffer ends with a newline
        has_trailing_newline = buf.endswith("\n")
        lines_list = buf.split("\n")
        
        # If there's no trailing newline, the last element of lines_list is a partial line.
        # We adjust state["offset"] backwards by the byte length of that partial line so we read it next time.
        if not has_trailing_newline and lines_list:
            partial_line = lines_list.pop()
            try:
                partial_bytes_len = len(partial_line.encode("utf-8"))
                state["offset"] -= partial_bytes_len
            except Exception:
                pass

        by_date = {}  # date_str -> list of log lines
        for line in lines_list:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                uid = record.get("uuid")
                if uid:
                    if uid in _seen_uuids:
                        continue  # already logged — replayed record from /cd
                    _seen_uuids[uid] = _seen_uuid_counter
                    _seen_uuid_counter += 1
                    # Trim oldest when over limit
                    if len(_seen_uuids) > _SEEN_UUIDS_MAX * 2:
                        cutoff = _seen_uuid_counter - _SEEN_UUIDS_MAX
                        _seen_uuids = {
                            u: c for u, c in _seen_uuids.items() if c >= cutoff
                        }
                ts = record.get("timestamp", "")
                if ts:
                    _last_record_ts = ts
            except json.JSONDecodeError:
                continue
            rec_lines = _record_to_lines(record, _id2name)
            if rec_lines:
                by_date.setdefault(_record_local_date(ts), []).extend(rec_lines)
            _check_auto_panic_safe(record)
        for date_str, lines in by_date.items():
            _append_log(date_str, "\n".join(lines))
        _save_state()
    except Exception as e:
        _diagnostic_log(f"JSONL_FLUSH_ERROR: {e}")


# -- main tick ----------------------------------------------------------------


def _tick():
    global _tick_active
    if _tick_active:
        return
    _tick_active = True
    threading.Thread(target=_tick_background, daemon=True).start()


def _tick_background():
    global _last_cleanup_time, _tick_active
    current_time = time.time()
    if current_time - _last_cleanup_time > 9000:
        _cleanup_old_screenshots()
        _last_cleanup_time = current_time
    
    projects = Path(_PROJECTS_DIR)
    if projects.exists():
        try:
            for slug_dir in projects.iterdir():
                if not slug_dir.is_dir():
                    continue
                for jf in slug_dir.glob("*.jsonl"):
                    _flush_jsonl(str(jf))
        except OSError:
            pass
    
    # Discover and flush Gemini JSONL chat files
    gemini_base = Path(os.path.expanduser("~/.gemini/tmp"))
    if gemini_base.exists():
        try:
            for proj_dir in gemini_base.iterdir():
                if not proj_dir.is_dir():
                    continue
                chats_dir = proj_dir / "chats"
                if chats_dir.exists() and chats_dir.is_dir():
                    for jf in chats_dir.glob("*.jsonl"):
                        _flush_jsonl(str(jf))
        except OSError:
            pass

    _SS_KEY = "__screenshot__"
    if _SS_KEY not in _last_screenshot_time:
        _last_screenshot_time[_SS_KEY] = current_time
    elif current_time - _last_screenshot_time[_SS_KEY] > _SCREENSHOT_INTERVAL:
        _last_screenshot_time[_SS_KEY] = current_time
        try:
            _take_screenshot(_SS_KEY)
        except Exception as e:
            _diagnostic_log(f"SCREENSHOT_BG_ERROR: {e}")

    # Re-schedule on the main thread
    sublime.set_timeout(_tick_schedule, _CHECK_MS)


def _tick_schedule():
    global _tick_active
    _tick_active = False
    _tick()


# -- commands -----------------------------------------------------------------


class AiCaptureScrollPositionCommand(sublime_plugin.TextCommand):
    """Capture a screenshot of the active view at the current scroll position and save it to the screenshot directory.

    Key binding: ctrl+alt+s.
    Menu: Main.sublime-menu → Tools → Ai Utilities — "Screenshot at Scroll".
    Command palette: "Ai: Screenshot at Scroll".
    """

    def run(self, edit):
        try:
            visible_region = self.view.visible_region()
            row, _ = self.view.rowcol(visible_region.begin())
            os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            fp = os.path.join(_SCREENSHOT_DIR, f"ai_scroll_line{row+1:04d}_{ts}.png")
            _screenshot_via_mcp(fp)
            msg = f"Screenshot at line {row+1}: {fp}"
            print(msg)
            sublime.status_message(msg)
            _diagnostic_log(f"MANUAL_SCREENSHOT: {fp}")
        except Exception as e:
            _diagnostic_log(f"SCREENSHOT_ERROR: {e}")
            sublime.error_message(f"Failed to capture screenshot: {e}")


# -- lifecycle ----------------------------------------------------------------


def plugin_loaded():
    global _tick_active
    _load_state()
    os.makedirs(_LOG_DIR, exist_ok=True)
    _cleanup_old_screenshots()
    if not _tick_active:
        _tick_active = True
        sublime.set_timeout(_tick, _CHECK_MS)
    msg = (
        f"ai_logger: initialized (JSONL mode, polling every {_CHECK_MS}ms, "
        f"screenshots every {_SCREENSHOT_INTERVAL}s)"
    )
    print(msg)
    _diagnostic_log(msg)


def plugin_unloaded():
    path = _find_active_transcript()
    if path:
        _flush_jsonl(path)
    _save_state()
    _diagnostic_log("ai_logger: unloaded")
