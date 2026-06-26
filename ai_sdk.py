"""ai_sdk.py — Claude via Agent SDK, inline conversation view.

Ctrl+Alt+A  — open / focus the Ai (SDK) view and enter input mode
Ctrl+Alt+X  — restart bridge (clears conversation history)
Enter       — submit prompt when in input mode
Socket      127.0.0.1:9503 — MCP eval server (ST tools for the agent)
Bridge      127.0.0.1:9504 — persistent ClaudeSDKClient (multi-turn)
"""

import json
import os
import re
import socket
import subprocess
import threading

import sublime
import sublime_plugin

_VIEW_NAME = "Ai (SDK)"
_PYTHON = r"C:\Users\donal\AppData\Local\Programs\Python\Python312\python.exe"
_SCRIPT = r"C:\Users\donal\projects\tools\agent_query.py"

_SOCKET_HOST = "127.0.0.1"
_SOCKET_PORT = 9503
_BRIDGE_PORT = 9504

_INPUT_REGION = "ai_sdk_input"
_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# ─── Module state ─────────────────────────────────────────────────────────────

_server = None
_bridge = None
_bridge_cwd = None
_working = False
_spinner_frame = 0


# ─── Socket server (ST eval for the agent) ────────────────────────────────────


def _eval_in_st(code):
    if "return " in code:
        lines = code.split("\n")
        new_lines = []
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("return "):
                indent = line[: len(line) - len(stripped)]
                new_lines.append(f"{indent}__result__ = {stripped[7:]}")
            else:
                new_lines.append(line)
        code = "__result__ = None\n" + "\n".join(new_lines)
    else:
        code = f"__result__ = None\n{code}"
    g = {"sublime": sublime, "sublime_plugin": sublime_plugin, "os": os}
    exec(code, g)
    return g.get("__result__")


class _SdkSocketServer:
    def __init__(self):
        self._sock = None
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._serve, daemon=True).start()

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def _serve(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((_SOCKET_HOST, _SOCKET_PORT))
            self._sock.listen(5)
            self._sock.settimeout(1.0)
            print(f"[ai_sdk] eval server on {_SOCKET_HOST}:{_SOCKET_PORT}")
            while self._running:
                try:
                    conn, _ = self._sock.accept()
                    threading.Thread(
                        target=self._handle, args=(conn,), daemon=True
                    ).start()
                except socket.timeout:
                    continue
                except Exception:
                    if self._running:
                        raise
        except Exception as e:
            print(f"[ai_sdk] eval server error: {e}")

    def _handle(self, conn):
        try:
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
            req = json.loads(data.strip())
            code = req.get("code", "")
            result = {"result": None, "error": None}
            done = threading.Event()

            def do_eval():
                try:
                    result["result"] = _eval_in_st(code)
                except Exception as exc:
                    result["error"] = str(exc)
                finally:
                    done.set()

            sublime.set_timeout(do_eval, 0)
            if not done.wait(timeout=10.0):
                result["error"] = "eval timed out after 10s"
            conn.sendall((json.dumps(result) + "\n").encode())
        except Exception as e:
            try:
                conn.sendall((json.dumps({"error": str(e)}) + "\n").encode())
            except Exception:
                pass
        finally:
            conn.close()


# ─── Persistent bridge ────────────────────────────────────────────────────────


class _Bridge:
    def __init__(self, cwd=None):
        self._cwd = cwd
        self._proc = None
        self._ready = threading.Event()
        self._current_sock = None
        self._stop_requested = False

    def stop_query(self):
        self._stop_requested = True
        sock = self._current_sock
        if sock:
            try:
                sock.sendall((json.dumps({"type": "interrupt"}) + "\n").encode())
            except Exception:
                pass

    def start(self):
        self._proc = subprocess.Popen(
            [_PYTHON, _SCRIPT, "--bridge", str(_BRIDGE_PORT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            cwd=self._cwd,
        )
        threading.Thread(target=self._monitor, daemon=True).start()
        print(f"[ai_sdk] bridge starting (port {_BRIDGE_PORT})")

    def _monitor(self):
        for line in self._proc.stdout:
            line = line.rstrip()
            print(f"[ai_sdk bridge] {line}")
            if "bridge connected" in line or "listening on" in line:
                self._ready.set()

    def stop(self):
        self._ready.clear()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    def restart(self):
        self.stop()
        self.start()

    def is_alive(self):
        return self._proc is not None and self._proc.poll() is None

    def send(self, prompt, on_event):
        threading.Thread(
            target=self._send_thread, args=(prompt, on_event), daemon=True
        ).start()

    def send_request(self, req, on_event):
        threading.Thread(
            target=self._send_thread, args=(None, on_event, req), daemon=True
        ).start()

    def _send_thread(self, prompt, on_event, req=None):
        if not self._ready.wait(timeout=60.0):
            on_event({"type": "error", "error": "Bridge did not connect within 60s"})
            return
        if req is None:
            req = {"id": 1, "prompt": prompt}
        req_line = json.dumps(req) + "\n"
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((_SOCKET_HOST, _BRIDGE_PORT))
            self._current_sock = sock
            self._stop_requested = False
            sock.sendall(req_line.encode())
            buf = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    if self._stop_requested:
                        on_event({"type": "stopped"})
                        return
                    event = json.loads(line)
                    on_event(event)
                    if event.get("type") in ("done", "error", "status_data"):
                        return
            # EOF path (recv returned b""): check if we were stopped
            if self._stop_requested:
                on_event({"type": "stopped"})
        except Exception as e:
            if self._stop_requested:
                on_event({"type": "stopped"})
            else:
                on_event({"type": "error", "error": str(e)})
        finally:
            self._current_sock = None
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass


# ─── Plugin lifecycle ─────────────────────────────────────────────────────────


def plugin_loaded():
    pass


def plugin_unloaded():
    global _server, _bridge
    if _server:
        _server.stop()
        _server = None
    if _bridge:
        _bridge.stop()
        _bridge = None


def _ensure_bridge():
    """Start server + bridge if not already running."""
    global _server, _bridge
    if _server is None:
        _server = _SdkSocketServer()
        _server.start()
    if _bridge is None or not _bridge.is_alive():
        _bridge = _Bridge(_bridge_cwd)
        _bridge.start()


# ─── View helpers ─────────────────────────────────────────────────────────────


def _find_sdk_view(window):
    for v in window.views():
        if v.name() == _VIEW_NAME or v.settings().get("ai_sdk_view"):
            return v
    return None


def _sdk_view(window):
    v = _find_sdk_view(window)
    if v:
        return v
    v = window.new_file()
    v.set_name(_VIEW_NAME)
    v.set_scratch(True)
    v.settings().set("word_wrap", True)
    v.settings().set("gutter", True)
    v.settings().set("line_numbers", False)
    v.settings().set("fold_buttons", True)
    v.settings().set("fade_fold_buttons", False)
    v.settings().set("ai_sdk_view", True)
    v.settings().set("ai_sdk_input_mode", False)
    v.set_read_only(False)
    v.run_command("append", {"characters": "\n"})
    v.set_read_only(True)
    return v


def _vwrite(view, text):
    """Append text to the (read-only) history area. Safe to call from any thread."""

    def _do(t=text):
        view.set_read_only(False)
        view.run_command("append", {"characters": t, "scroll_to_end": True})
        view.set_read_only(True)

    sublime.set_timeout(_do, 0)


# ─── Spinner ──────────────────────────────────────────────────────────────────


def _start_spinner(window):
    global _working, _spinner_frame
    _working = True
    _spinner_frame = 0
    _animate(window)


def _animate(window):
    global _spinner_frame
    if not _working:
        return
    v = _find_sdk_view(window)
    if v:
        frame = _SPINNER_FRAMES[_spinner_frame % len(_SPINNER_FRAMES)]
        v.set_name(f"{frame} {_VIEW_NAME}")
        _spinner_frame += 1
    sublime.set_timeout(lambda: _animate(window), 200)


def _stop_spinner(window):
    global _working
    _working = False
    v = _find_sdk_view(window)
    if v:
        v.set_name(f"◇ {_VIEW_NAME}")


# ─── ccstatusline phantom (below input line) ──────────────────────────────────

_STATUS_PHANTOM_KEY = "ai_sdk_ccstatus"
_ccstatus_phantom_sets = {}


def _ansi256_hex(n):
    """Convert a 256-colour palette index to a CSS #rrggbb string."""
    STD16 = [
        "#1e1e2e", "#f38ba8", "#a6e3a1", "#f9e2af",
        "#89b4fa", "#cba6f7", "#94e2d5", "#cdd6f4",
        "#585b70", "#f38ba8", "#a6e3a1", "#f9e2af",
        "#89b4fa", "#cba6f7", "#94e2d5", "#cdd6f4",
    ]
    if n < 16:
        return STD16[n]
    if n >= 232:
        v = 8 + (n - 232) * 10
        return f"#{v:02x}{v:02x}{v:02x}"
    n -= 16
    b, g, r = n % 6, (n // 6) % 6, n // 36
    def _c(x): return 0 if x == 0 else 55 + x * 40
    return f"#{_c(r):02x}{_c(g):02x}{_c(b):02x}"


def _ansi_to_html(text):
    """Convert ANSI colour sequences in text to miniHTML <span> tags."""
    parts = re.split(r"(\x1b\[[0-9;]*m)", text)
    out, color = [], None
    for p in parts:
        if p.startswith("\x1b[") and p.endswith("m"):
            codes = p[2:-1].split(";")
            i = 0
            while i < len(codes):
                c = codes[i]
                if c in ("", "0", "39"):
                    color = None
                elif c == "38" and i + 1 < len(codes):
                    if codes[i + 1] == "5" and i + 2 < len(codes):
                        color = _ansi256_hex(int(codes[i + 2]))
                        i += 2
                    elif codes[i + 1] == "2" and i + 4 < len(codes):
                        r2, g2, b2 = int(codes[i+2]), int(codes[i+3]), int(codes[i+4])
                        color = f"#{r2:02x}{g2:02x}{b2:02x}"
                        i += 4
                i += 1
        elif p:
            esc = (p.replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;").replace("\xa0", " "))
            if color:
                out.append(f'<span style="color:{color}">{esc}</span>')
            else:
                out.append(esc)
    return "".join(out)


def _get_ccstatus_cmd():
    try:
        path = os.path.expanduser("~/.claude/settings.json")
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        cmd = s.get("statusLine", {}).get("command", "")
        return cmd.split() if cmd else []
    except Exception:
        return []


def _update_ccstatus(view, event):
    def _bg():
        cmd = _get_ccstatus_cmd()
        if not cmd:
            return
        cost = event.get("cost") or 0.0
        window = view.window()
        cwd = ""
        if window:
            folders = window.folders()
            if folders:
                cwd = folders[0].replace("\\", "/")
        model = event.get("model") or sublime.load_settings(
            "ClaudeCode.sublime-settings"
        ).get("default_model", "claude-sonnet-4-6")
        if model == "sonnet":
            model = "claude-sonnet-4-6"
        elif model == "opus":
            model = "claude-opus-4-8"
        data = {
            "hook_event_name": "PostToolUse",
            "session_id": "sdk",
            "model": model,
            "cost": {"total_cost_usd": cost},
            "cwd": cwd,
        }
        cw = event.get("context_window")
        if cw:
            data["context_window"] = cw
        try:
            result = subprocess.run(
                cmd,
                input=json.dumps(data).encode("utf-8"),
                capture_output=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            lines = result.stdout.decode("utf-8", errors="replace").splitlines()
            lines = [l for l in lines if l.strip()]
        except Exception as e:
            print(f"[ai_sdk] ccstatusline: {e}")
            return
        if not lines:
            return
        html_lines = [_ansi_to_html(l.rstrip()) for l in lines if l.strip()]
        content = "<br>".join(html_lines)
        html = f'<body style="background-color:#262626;margin:0;padding:3px 6px"><span style="font-size:0.85em;color:#d8dee9">{content}</span></body>'

        def _do():
            view.erase_phantoms(_STATUS_PHANTOM_KEY)
            view.erase_status(_STATUS_PHANTOM_KEY)
            input_start = view.settings().get("ai_sdk_input_start")
            pt = max(0, (input_start - 2) if input_start else view.size())
            ps = _ccstatus_phantom_sets.get(view.id())
            if ps is None:
                ps = sublime.PhantomSet(view, _STATUS_PHANTOM_KEY)
                _ccstatus_phantom_sets[view.id()] = ps
            ps.update([sublime.Phantom(sublime.Region(pt), html, sublime.LAYOUT_BELOW)])

        sublime.set_timeout(_do, 0)

    threading.Thread(target=_bg, daemon=True).start()


# ─── Conversation rendering ───────────────────────────────────────────────────


def _render_prompt(view, prompt):
    """Complete the user's typed input line with ▶ marker."""

    def _do():
        view.set_read_only(False)
        view.run_command("append", {"characters": " ▶\n\n", "scroll_to_end": True})
        view.set_read_only(True)

    sublime.set_timeout(_do, 0)


def _render_tool_start(view, name, tool_id):
    """Append a pending tool line and record the ⚙ position."""

    def _do(n=name, tid=tool_id):
        view.set_read_only(False)
        pos = view.size()
        view.run_command("append", {"characters": f"  ⚙ {n}\n", "scroll_to_end": True})
        # Track position of ⚙ (offset 2 past pos, 1 character wide)
        view.add_regions(
            f"ai_sdk_tool_{tid}",
            [sublime.Region(pos + 2, pos + 3)],
            flags=sublime.HIDDEN,
        )
        view.set_read_only(True)

    sublime.set_timeout(_do, 0)


def _render_tool_result(view, tool_id, is_error):
    """Flip ⚙ → ✔ or ✘ for the completed tool."""

    def _do(tid=tool_id, err=is_error):
        key = f"ai_sdk_tool_{tid}"
        regions = view.get_regions(key)
        if regions:
            symbol = "✘" if err else "✔"
            view.run_command(
                "ai_sdk_replace",
                {
                    "start": regions[0].begin(),
                    "end": regions[0].end(),
                    "text": symbol,
                },
            )
            view.erase_regions(key)

    sublime.set_timeout(_do, 0)


def _render_meta(view, event):
    """Append @done footer after a response."""

    def _do():
        dur_s = (event.get("duration_ms") or 0) / 1000.0
        cost = event.get("cost") or 0.0
        turns = event.get("num_turns") or 0
        stop = event.get("stop_reason") or ""
        parts = [f"{dur_s:.1f}s"]
        if cost > 0:
            parts.append(f"${cost:.4f}")
        if turns:
            parts.append(f"{turns} turns")
        if stop and stop != "end_turn":
            parts.append(stop)
        meta = f"\n@done({', '.join(parts)})\n"
        view.set_read_only(False)
        view.run_command("append", {"characters": meta, "scroll_to_end": True})
        view.set_read_only(True)

    sublime.set_timeout(_do, 0)


# ─── Input mode ───────────────────────────────────────────────────────────────


def _enter_input_mode(view, window):
    def _do():
        view.set_read_only(False)
        view.run_command("append", {"characters": "\n◎ ", "scroll_to_end": True})
        pos = view.size()
        # Store start as integer setting — regions shift when text is inserted at them
        view.settings().set("ai_sdk_input_start", pos)
        view.sel().clear()
        view.sel().add(sublime.Region(pos, pos))
        view.show(pos)
        view.settings().set("ai_sdk_input_mode", True)
        if window:
            window.focus_view(view)
        v = _find_sdk_view(window)
        if v:
            v.set_name(f"◇ {_VIEW_NAME}")

    sublime.set_timeout(_do, 0)


def _exit_input_mode(view):
    """Lock the view and clear input mode. Call from main thread."""
    view.settings().erase("ai_sdk_input_start")
    view.settings().set("ai_sdk_input_mode", False)
    view.set_read_only(True)


def _get_input_text(view):
    """Return whatever the user typed in the input area."""
    start = view.settings().get("ai_sdk_input_start", -1)
    if start < 0:
        return ""
    return view.substr(sublime.Region(start, view.size())).strip()


# ─── Event dispatch ───────────────────────────────────────────────────────────


def _on_event(view, window, event):
    """Route one bridge event to the appropriate renderer. Called from bg thread."""
    t = event.get("type")
    if t == "text":
        _vwrite(view, event.get("text", ""))
    elif t == "tool_use":
        _render_tool_start(view, event.get("name", "?"), event.get("tool_id", ""))
    elif t == "tool_result":
        _render_tool_result(
            view, event.get("tool_id", ""), event.get("is_error", False)
        )
    elif t == "done":
        if event.get("stop_reason") == "interrupted":
            _vwrite(view, "\nInterrupted\n")
        else:
            _render_meta(view, event)
        _update_ccstatus(view, event)
        sublime.set_timeout(lambda: _stop_spinner(window), 0)
        sublime.set_timeout(lambda: _enter_input_mode(view, window), 50)
    elif t == "stopped":
        _vwrite(view, "\n[Stopped]\n")
        _update_ccstatus(view, event)
        sublime.set_timeout(lambda: _stop_spinner(window), 0)
        sublime.set_timeout(lambda: _enter_input_mode(view, window), 50)
    elif t == "error":
        err = event.get("error", "unknown error")
        _vwrite(view, f"\n[Error: {err}]\n")
        sublime.set_timeout(lambda: _stop_spinner(window), 0)
        sublime.set_timeout(lambda: _enter_input_mode(view, window), 50)


# ─── View event listener (intercepts Enter in input mode) ────────────────────


class AiSdkViewListener(sublime_plugin.ViewEventListener):
    @classmethod
    def is_applicable(cls, settings):
        return settings.get("ai_sdk_view", False)

    def on_text_command(self, command, args):
        if (
            command == "insert"
            and args.get("characters") == "\n"
            and self.view.settings().get("ai_sdk_input_mode", False)
        ):
            return ("ai_sdk_submit", {})
        return None

    def on_close(self):
        global _server, _bridge
        if _bridge:
            _bridge.stop()
            _bridge = None
        if _server:
            _server.stop()
            _server = None


# ─── Helper TextCommand ───────────────────────────────────────────────────────


class AiSdkReplaceCommand(sublime_plugin.TextCommand):
    """Replace a region in the view (handles read-only toggle)."""

    def run(self, edit, start, end, text):
        self.view.set_read_only(False)
        self.view.replace(edit, sublime.Region(start, end), text)
        self.view.set_read_only(True)


# ─── Commands ─────────────────────────────────────────────────────────────────


class AiSdkFocusCommand(sublime_plugin.WindowCommand):
    """Ctrl+Alt+A — open / focus the Ai (SDK) conversation view."""

    def run(self):
        _ensure_bridge()
        view = _sdk_view(self.window)
        self.window.focus_view(view)
        if not view.settings().get("ai_sdk_input_mode", False):
            _enter_input_mode(view, self.window)
        _update_ccstatus(view, {})


class AiSdkSubmitCommand(sublime_plugin.TextCommand):
    """Enter — submit the typed prompt to Claude."""

    def run(self, edit):
        view = self.view
        window = view.window()
        prompt = _get_input_text(view)
        if not prompt:
            return
        # Defer all state changes until after this edit context closes
        sublime.set_timeout(lambda: _do_submit(view, window, prompt), 10)

    def is_enabled(self):
        return bool(self.view.settings().get("ai_sdk_input_mode", False))


class AiSdkNoopCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        pass


class AiSdkStopCommand(sublime_plugin.WindowCommand):
    """Stop the running Claude query."""

    def run(self):
        global _bridge
        if _bridge:
            _bridge.stop_query()


class AiSdkKeyInterceptor(sublime_plugin.EventListener):
    """Intercept Ctrl+C inside the AI(SDK) view. Esc is handled via keymap context."""

    def on_text_command(self, view, command_name, args):
        if command_name == "copy":
            if view.settings().get("ai_sdk_view") and not view.settings().get("ai_sdk_input_mode"):
                view.window().run_command("ai_sdk_stop")
                return ("ai_sdk_noop", {})
        return None


# ─── Status renderer ─────────────────────────────────────────────────────────


def _render_status(view, window, event):
    ctx = event.get("context", {})
    servers = event.get("servers", [])
    L = ["", "▶ Session"]
    L.append(f"    Model:      {ctx.get('model', 'unknown')}")
    session_id = ctx.get("session_id", "")
    if session_id:
        L.append(f"    Session ID: {session_id}")
    L.append(f"    Bridge:     running, port {_BRIDGE_PORT}")
    api = ctx.get("api_usage") or {}
    if api:
        cost = api.get("total_cost_usd") or api.get("cost")
        if cost is not None:
            L.append(f"    Session cost: ${cost:.4f}")
    L.append("")

    total = ctx.get("total_tokens", 0)
    mx = ctx.get("max_tokens", 0)
    raw_mx = ctx.get("raw_max_tokens", 0)
    pct = ctx.get("percent", 0)
    ac_on = ctx.get("autocompact_enabled", False)
    ac_thresh = ctx.get("autocompact_threshold")
    L.append(f"▶ Context  ({pct}% used)")
    L.append(f"    Used:        {total:>7,} / {mx:,} (effective max)")
    if raw_mx and raw_mx != mx:
        L.append(f"    Model max:   {raw_mx:>7,}")
    if ac_thresh:
        L.append(
            f"    Autocompact: {'enabled' if ac_on else 'disabled'}  threshold {ac_thresh:,}"
        )
    else:
        L.append(f"    Autocompact: {'enabled' if ac_on else 'disabled'}")
    L.append("")
    L.append("    ▶ Token breakdown")
    for cat in ctx.get("categories", []):
        deferred = " (deferred)" if cat.get("deferred") else ""
        L.append(f"        {cat['name']:<32} {cat['tokens']:>7,}{deferred}")
    sps = ctx.get("system_prompt_sections", [])
    if sps:
        L.append("")
        L.append("    ▶ System prompt sections")
        for s in sps:
            L.append(f"        {s.get('name', '?'):<32} {s.get('tokens', 0):>7,}")
    st_tools = ctx.get("system_tools", [])
    if st_tools:
        L.append("")
        L.append("    ▶ Built-in tools")
        for t in st_tools:
            L.append(f"        {t.get('name', '?'):<32} {t.get('tokens', 0):>7,}")
    mcp_tools = ctx.get("mcp_tools", [])
    if mcp_tools:
        L.append("")
        L.append("    ▶ MCP tool tokens")
        for t in mcp_tools:
            loaded = "" if t.get("isLoaded", True) else " (deferred)"
            L.append(
                f"        {t.get('name', '?'):<32} {t.get('tokens', 0):>7,}{loaded}"
            )
    mem = ctx.get("memory_files", [])
    if mem:
        L.append("")
        L.append("    ▶ Memory files")
        for f in mem:
            L.append(f"        {f.get('path', '?'):<40} {f.get('tokens', 0):>7,}")
    L.append("")

    connected = sum(1 for s in servers if s["status"] == "connected")
    needs_auth = sum(1 for s in servers if s["status"] == "needs-auth")
    failed = sum(1 for s in servers if s["status"] == "failed")
    parts = [f"{connected} connected"]
    if needs_auth:
        parts.append(f"{needs_auth} needs-auth")
    if failed:
        parts.append(f"{failed} failed")
    L.append(f"▶ MCP Servers  ({', '.join(parts)})")
    for s in servers:
        icon = "✔" if s["status"] == "connected" else "✘"
        tools = s.get("tools", [])
        err = f"  — {s['error']}" if s.get("error") else ""
        L.append(
            f"    {icon} {s['name']:<26} {s['status']:<12} {len(tools)} tools{err}"
        )
        if s.get("version"):
            L.append(f"        version:  {s['version']}")
        if s.get("config_type"):
            L.append(f"        type:     {s['config_type']}  {s.get('config_url', '')}")
        if s.get("scope"):
            L.append(f"        scope:    {s['scope']}")
        if tools:
            L.append("        ▶ Tools")
            for t in tools:
                flags = []
                if t.get("readonly"):
                    flags.append("ro")
                if t.get("destructive"):
                    flags.append("destructive")
                flag_str = f"  [{', '.join(flags)}]" if flags else ""
                desc = t.get("description", "")
                desc_str = f"  {desc[:60]}" if desc else ""
                L.append(f"            {t['name']}{flag_str}{desc_str}")
    L.append("")
    _vwrite(view, "\n".join(L))
    sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)


# ─── Slash command handler ────────────────────────────────────────────────────

_SLASH_COMMANDS = {
    "clear": "Restart bridge and clear conversation history",
    "help": "Show available slash commands",
    "status": "Show bridge status",
    "tools": "List allowed tools",
    "compact": "Ask Claude to summarize conversation context (forwarded)",
}


def _handle_slash(view, window, prompt):
    """Return True if handled locally, False to forward to bridge."""
    parts = prompt[1:].split(None, 1)
    cmd = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "help":
        lines = ["", "Slash commands:", ""]
        for name, desc in _SLASH_COMMANDS.items():
            lines.append(f"  /{name:<10} {desc}")
        lines.append("")
        _vwrite(view, "\n".join(lines))
        _enter_input_mode(view, window)
        return True

    if cmd == "clear":
        global _bridge
        _vwrite(view, "\n─── bridge restarted — history cleared ───\n")
        threading.Thread(target=_bridge.restart, daemon=True).start()
        sublime.set_timeout(lambda: _enter_input_mode(view, window), 500)
        return True

    if cmd == "status":
        if not (_bridge and _bridge.is_alive()):
            _vwrite(view, "\nBridge: stopped\n")
            _enter_input_mode(view, window)
            return True

        def on_status(event):
            if event.get("type") == "status_data":
                _render_status(view, window, event)
            elif event.get("type") == "error":
                _vwrite(view, f"\n[Status error: {event.get('error')}]\n")
                sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)

        _bridge.send_request({"id": 1, "type": "status_request"}, on_status)
        return True

    if cmd == "tools":
        import importlib.util

        spec = importlib.util.spec_from_file_location("agent_query", _SCRIPT)
        aq = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(aq)
        lines = ["", "Allowed tools:", ""]
        for t in aq._ALLOWED_TOOLS:
            lines.append(f"  {t}")
        lines.append("")
        _vwrite(view, "\n".join(lines))
        _enter_input_mode(view, window)
        return True

    # Unknown command — forward to bridge (handles /compact, /loop, etc.)
    return False


def _do_submit(view, window, prompt):
    _exit_input_mode(view)
    _render_prompt(view, prompt)

    if prompt.startswith("/"):
        if _handle_slash(view, window, prompt):
            _stop_spinner(window)
            return

    _start_spinner(window)
    if _bridge and _bridge.is_alive():
        _bridge.send(prompt, lambda ev: _on_event(view, window, ev))
    else:
        _vwrite(view, "[Bridge not ready — try Ctrl+Alt+X to restart]\n")
        _enter_input_mode(view, window)


class AiSdkClearCommand(sublime_plugin.WindowCommand):
    """Ctrl+Alt+X — restart bridge (clears conversation history)."""

    def run(self):
        global _bridge
        if not _bridge:
            return
        view = _sdk_view(self.window)
        if view.settings().get("ai_sdk_input_mode", False):
            _exit_input_mode(view)
        _vwrite(view, "\n─── bridge restarted — history cleared ───\n")
        threading.Thread(target=_bridge.restart, daemon=True).start()
        sublime.set_timeout(lambda: _enter_input_mode(view, self.window), 500)


class AiSdkOpenHereCommand(sublime_plugin.WindowCommand):
    """Sidebar: open AI(SDK) panel with cwd set to the chosen directory."""

    def run(self, paths=None):
        import os as _os
        global _bridge, _bridge_cwd
        paths = paths or []
        path = paths[0] if paths else None
        if path and not _os.path.isdir(path):
            path = _os.path.dirname(path)
        if not path:
            folders = self.window.folders()
            path = folders[0] if folders else None
        if not path:
            return
        _bridge_cwd = path
        _ensure_bridge()
        view = _sdk_view(self.window)
        self.window.focus_view(view)
        if view.settings().get("ai_sdk_input_mode", False):
            _exit_input_mode(view)
        _vwrite(view, f"\n─── cwd: {path} ───\n")
        if _bridge:
            _bridge._cwd = path
            threading.Thread(target=_bridge.restart, daemon=True).start()
        sublime.set_timeout(lambda: _enter_input_mode(view, self.window), 500)

    def is_visible(self, paths=None):
        return True


class AiSdkOpenInEditorCommand(sublime_plugin.TextCommand):
    """Context/tab menu: open AI(SDK) panel with cwd set to current file's directory."""

    def run(self, edit):
        import os as _os
        global _bridge, _bridge_cwd
        path = self.view.file_name()
        if path:
            path = _os.path.dirname(path)
        else:
            window = self.view.window()
            folders = window.folders() if window else []
            path = folders[0] if folders else None
        if not path:
            return
        _bridge_cwd = path
        _ensure_bridge()
        window = self.view.window()
        view = _sdk_view(window)
        window.focus_view(view)
        if view.settings().get("ai_sdk_input_mode", False):
            _exit_input_mode(view)
        _vwrite(view, f"\n─── cwd: {path} ───\n")
        if _bridge:
            _bridge._cwd = path
            threading.Thread(target=_bridge.restart, daemon=True).start()
        sublime.set_timeout(lambda: _enter_input_mode(view, window), 500)
