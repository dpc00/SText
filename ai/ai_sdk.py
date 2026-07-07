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
_USE_OLLAMA = True
_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
_OLLAMA_SCRIPT = os.path.join(_BACKEND, "agent_query_ollama.py")
_PYTHON = r"C:\Users\donal\AppData\Local\Programs\Python\Python312\python.exe"
_SCRIPT = os.path.join(_BACKEND, "agent_query.py")

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

# Views currently in a streaming burst. Toggling read_only per-token in a
# burst causes races where one callback's set_read_only(True) lands between
# another's set_read_only(False)/append and silently drops the write (the
# "3-token response" bug). During a burst we leave the view writable and
# only re-lock it on `done`. The set is keyed by view.id() to be per-tab.
_streaming_views = set()

# Views that have already received their first thinking chunk in the
# current burst. The thinking marker ('  💭 ') should be written only
# once at the start of the thinking block; subsequent deltas just append
# text so we don't get '💭 The user  💭 is...' with a marker on every
# chunk. Cleared on done/stopped/error along with _streaming_views.
_thinking_started = set()


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
            [
                _PYTHON,
                _OLLAMA_SCRIPT if _USE_OLLAMA else _SCRIPT,
                "--bridge",
                str(_BRIDGE_PORT),
            ],
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
        # Two checks: (1) our local subprocess reference is alive, or
        # (2) the bridge port is reachable. After a plugin reload, our
        # subprocess reference may be the freshly-spawned one that died
        # on port-in-use, but the *real* bridge (the older subprocess
        # that survived) is still on the port. The second check catches
        # that case.
        if self._proc is not None and self._proc.poll() is None:
            return True
        return _port_in_use(_BRIDGE_PORT)

    def send(self, prompt, on_event):
        threading.Thread(
            target=self._send_thread, args=(prompt, on_event), daemon=True
        ).start()

    def send_request(self, req, on_event):
        threading.Thread(
            target=self._send_thread, args=(None, on_event, req), daemon=True
        ).start()

    def send_raw(self, msg):
        """Send a raw JSON line on the current query socket (for HIL approvals).

        Reuses the in-flight socket so the bridge's socket watcher receives it
        on the same connection as the query it's gating.
        """
        sock = self._current_sock
        if sock:
            try:
                sock.sendall((json.dumps(msg) + "\n").encode())
            except Exception:
                pass

    def _send_thread(self, prompt, on_event, req=None):
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
    # Tab title spinner was removed (see _start_spinner comment). On
    # reload, we need to also stop any *stale* _animate loops from
    # previous plugin instances that are still ticking in ST's
    # set_timeout queue. ST has no API to cancel queued callbacks, so
    # we patch the old module globals to make their _animate a no-op
    # and _working False. The stale lambdas (still in the queue from
    # previous reloads) will then call the no-op and stop writing
    # spinner characters to the view's name.
    import gc
    import sys as _sys
    current = _sys.modules.get(__name__)
    funcs = [obj for obj in gc.get_objects()
             if callable(obj) and hasattr(obj, "__module__")
             and obj.__module__ == "User.ai.ai_sdk"
             and obj.__name__ == "<lambda>"]
    for lam in funcs:
        g = lam.__globals__
        if g is current.__dict__:
            continue  # don't patch our own globals
        def _no_op(window, _g=g):
            return
        g["_animate"] = _no_op
        g["_working"] = False
        g["_spinner_frame"] = 0
    # Reset the SDK view's name to the canonical label.
    for w in sublime.windows():
        for v in w.views():
            if v.settings().get("ai_sdk_view"):
                v.set_name(_VIEW_NAME)
                break
    # Re-bind to any existing bridge subprocess. After a plugin reload,
    # the bridge subprocess (started by the previous instance) is
    # usually still running on port 9504 — but our local _bridge
    # wrapper was reset to None. _ensure_bridge probes the port and
    # reattaches if a listener responds. Without this, the first
    # prompt submission after a reload gets "Bridge not ready" until
    # something else triggers _ensure_bridge.
    try:
        _ensure_bridge()
    except Exception:
        pass  # non-fatal: bridge will be started on first submit


def plugin_unloaded():
    # Reset the SDK view's name on unload too.
    for w in sublime.windows():
        for v in w.views():
            if v.settings().get("ai_sdk_view"):
                v.set_name(_VIEW_NAME)
                break
    # Deliberately do NOT stop the bridge or socket server on unload.
    # They hold the conversation history (`messages` list) and the MCP
    # session state. Killing them on every plugin reload (which can
    # happen often in development) wipes all context and forces the
    # user to re-establish the conversation from scratch.
    # The processes are owned by this ST instance and will be cleaned
    # up when ST itself exits.
    pass


def _ensure_bridge():
    """Start server + bridge if not already running.

    Reuses an existing bridge on the port if one is reachable, even if our
    local subprocess reference is dead. This handles the case where the
    bridge survived a plugin reload (we deliberately don't kill it in
    plugin_unloaded, to preserve conversation history) but our wrapper
    object was reset.
    """
    global _server, _bridge
    if _server is None:
        _server = _SdkSocketServer()
        _server.start()

    # If our local bridge is alive and its cwd matches the module's
    # _bridge_cwd, nothing to do. If the wrapper has no cwd set but
    # _bridge_cwd is, apply it (the wrapper is fresh from a reload and
    # didn't pick up the cwd that was set before).
    if _bridge is not None and _bridge.is_alive():
        if _bridge_cwd and _bridge._cwd != _bridge_cwd:
            _bridge._cwd = _bridge_cwd
        return

    # Probe the bridge port: something may already be listening (an older
    # bridge subprocess that survived a plugin reload). If so, create a
    # bridge wrapper that just connects to it without spawning a new
    # subprocess. This avoids the port-in-use problem on Windows where
    # asyncio.start_server doesn't set SO_REUSEADDR.
    if _port_in_use(_BRIDGE_PORT):
        # Try to talk to it: send a no-op request, see if it responds.
        if _bridge_responds():
            if _bridge is None:
                _bridge = _Bridge(_bridge_cwd)
            return

    # Port is free (or the existing listener is dead) — start a new bridge.
    _bridge = _Bridge(_bridge_cwd)
    _bridge.start()


def _port_in_use(port):
    """True if something is already listening on localhost:port."""
    import socket as _s
    s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except Exception:
        return False


def _bridge_responds():
    """True if a bridge-like responder is on the port (replies to list_models)."""
    import socket as _s
    try:
        s = _s.create_connection(("127.0.0.1", _BRIDGE_PORT), timeout=2)
        s.sendall((json.dumps({"id": 1, "type": "list_models"}) + "\n").encode())
        s.settimeout(2)
        data = b""
        while b"\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        if not data:
            return False
        resp = json.loads(data.decode().split("\n", 1)[0])
        return resp.get("type") == "model_list"
    except Exception:
        return False


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
    """Append text to the history area. Safe to call from any thread.

    During a streaming burst (between the first text/thinking delta and the
    matching `done`), the view is kept writable as long as the burst lasts;
    we just append. The dispatcher unlocks/re-locks the view at the burst
    boundaries, so per-token toggles here would only race with each other.
    """

    def _do(t=text):
        if view.id() in _streaming_views:
            # Streaming: view is already writable. Just append.
            view.run_command("append", {"characters": t, "scroll_to_end": True})
        else:
            view.set_read_only(False)
            view.run_command("append", {"characters": t, "scroll_to_end": True})
            view.set_read_only(True)

    sublime.set_timeout(_do, 0)


# ─── Spinner ──────────────────────────────────────────────────────────────────
#
# Note: the tab title spinner was REMOVED. It cycled spinner characters
# (⠋⠙⠹…) into the view's `name` field, which is what ST shows in the
# tab strip. The user found this actively harmful — the moving char
# makes the tab hard to locate visually among the other static-named
# tabs. The view name is now set once at creation to "Ai (SDK)" and
# stays there. Activity is still indicated by:
#   1. the per-turn @done footer with timing/token info
#   2. the ccstatusline phantom below the input (model, cost, ctx %)
# The _start_spinner/_stop_spinner functions are kept as no-ops so
# callers don't break, but the view name is not touched.
#
# Why no-ops rather than deletion: a half-dozen code paths call
# _start_spinner/_stop_spinner; touching them all is a wider change
# than the user asked for. Removing the side effect (the set_name
# inside _animate) gets the user's win without the risk of breaking
# the call sites.
#
# Implementation note on the re-resolve: _animate_recheck looks up
# the *current* module's _animate and _working at call time. This
# way, stale set_timeout lambdas from previous plugin reloads call
# the *new* _animate (which is a no-op) and the spinner stops cleanly.
# Without this indirection, the closure would capture the old _animate
# and the spinner would loop forever using the old module's stale state.


def _start_spinner(window):
    return  # no-op: tab title spinner removed


def _animate(window):
    return  # no-op: tab title spinner removed


def _animate_recheck(window):
    """Re-scheduling trampoline. Always looks up the current module's
    _animate and _working via sys.modules, so stale callbacks from
    previous plugin reloads become no-ops."""
    import sys as _sys
    mod = _sys.modules.get(__name__)
    if mod is None:
        return
    fn = getattr(mod, "_animate", None)
    if fn is None:
        return
    fn(window)


def _stop_spinner(window):
    # Force the view name back to the canonical label. The user noticed
    # that a stale spinner character could remain in the tab title after
    # a response completes. This explicit reset is the safety net.
    v = _find_sdk_view(window)
    if v:
        v.set_name(_VIEW_NAME)


# ─── ccstatusline phantom (below input line) ──────────────────────────────────

_STATUS_PHANTOM_KEY = "ai_sdk_ccstatus"
_ccstatus_phantom_sets = {}


def _ansi256_hex(n):
    """Convert a 256-colour palette index to a CSS #rrggbb string."""
    STD16 = [
        "#1e1e2e",
        "#f38ba8",
        "#a6e3a1",
        "#f9e2af",
        "#89b4fa",
        "#cba6f7",
        "#94e2d5",
        "#cdd6f4",
        "#585b70",
        "#f38ba8",
        "#a6e3a1",
        "#f9e2af",
        "#89b4fa",
        "#cba6f7",
        "#94e2d5",
        "#cdd6f4",
    ]
    if n < 16:
        return STD16[n]
    if n >= 232:
        v = 8 + (n - 232) * 10
        return f"#{v:02x}{v:02x}{v:02x}"
    n -= 16
    b, g, r = n % 6, (n // 6) % 6, n // 36

    def _c(x):
        return 0 if x == 0 else 55 + x * 40

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
                        r2, g2, b2 = (
                            int(codes[i + 2]),
                            int(codes[i + 3]),
                            int(codes[i + 4]),
                        )
                        color = f"#{r2:02x}{g2:02x}{b2:02x}"
                        i += 4
                i += 1
        elif p:
            esc = (
                p.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\xa0", " ")
            )
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


def _summarize_input(name, input_dict):
    """Render a tool's input args as a short one-line summary.

    Used by _render_tool_start to give the user immediate context about
    what the model just invoked. No per-tool formatter — this works for
    all 237+ tools across every MCP server. If the dict is empty, returns
    just the tool name. If a string field is unusually long, it's
    truncated with an ellipsis.
    """
    if not input_dict:
        return name
    bits = []
    for k, v in input_dict.items():
        if isinstance(v, str):
            s = v if len(v) <= 80 else v[:77] + "…"
            bits.append(f"{k}={s!r}")
        elif isinstance(v, (int, float, bool)):
            bits.append(f"{k}={v}")
        elif isinstance(v, list):
            bits.append(f"{k}=[{len(v)} items]")
        elif isinstance(v, dict):
            bits.append(f"{k}={{{len(v)} keys}}")
        else:
            bits.append(f"{k}=…")
    return f"{name}  " + "  ".join(bits)


def _render_tool_start(view, name, tool_id, input_dict=None):
    """Append a pending tool line (⚙ + args) and record the ⚙ position.

    Prints the tool name plus a one-line summary of its input args so the
    user can see what the model invoked. No per-tool formatter needed —
    the summary is generated generically from the input dict.
    """

    def _do(n=name, tid=tool_id, inp=input_dict):
        view.set_read_only(False)
        pos = view.size()
        summary = _summarize_input(n, inp)
        line = f"  ⚙ {summary}\n"
        view.run_command("append", {"characters": line, "scroll_to_end": True})
        # Track position of ⚙ (offset 2 past pos, 1 character wide)
        view.add_regions(
            f"ai_sdk_tool_{tid}",
            [sublime.Region(pos + 2, pos + 3)],
            flags=sublime.HIDDEN,
        )
        view.set_read_only(True)

    sublime.set_timeout(_do, 0)


def _render_tool_result(view, tool_id, is_error, result_text=None):
    """Flip ⚙ → ✔ or ✘ for the completed tool, then dump the result.

    The result is printed on indented lines below the icon so the user
    can see what came back without expanding anything. If the result is
    long it gets truncated in the bridge before being sent. This is the
    ST-side equivalent of sublime-claude's per-tool formatters, but
    generic — works for all 237+ tools without a registry.
    """

    def _do(tid=tool_id, err=is_error, txt=result_text):
        key = f"ai_sdk_tool_{tid}"
        regions = view.get_regions(key)
        view.set_read_only(False)
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
        if txt:
            # Indent each line of the result for readability. Result
            # already has ellipsis truncation from the bridge if long.
            for line in txt.splitlines() or [txt]:
                view.run_command(
                    "append",
                    {
                        "characters": f"      {line}\n",
                        "scroll_to_end": True,
                    },
                )
        view.set_read_only(True)

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
        cw = event.get("context_window") or {}
        in_tok = cw.get("total_input_tokens", 0)
        out_tok = cw.get("total_output_tokens", 0)
        if in_tok or out_tok:
            parts.append(f"in {in_tok:,}/out {out_tok:,}")
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
            v.set_name(_VIEW_NAME)

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


def _prompt_approval(window, tool_id, name, args):
    """Pop ST's input panel asking y/n. Sends the answer on the in-flight
    query socket so the bridge's watcher unblocks the gated tool call."""
    def _show():
        def on_done(text):
            approve = text.strip().lower() in ("y", "yes")
            if _bridge:
                _bridge.send_raw(
                    {"type": "tool_approval", "tool_id": tool_id, "approve": approve}
                )

        def on_cancel():
            if _bridge:
                _bridge.send_raw(
                    {"type": "tool_approval", "tool_id": tool_id, "approve": False}
                )

        window.show_input_panel(
            f"Approve {name}? (y/n)", "", on_done, None, on_cancel
        )

    sublime.set_timeout(_show, 0)


def _on_event(view, window, event):
    """Route one bridge event to the appropriate renderer. Called from bg thread."""
    t = event.get("type")

    # Burst boundaries. The first content event in a response marks the view
    # as streaming (kept writable) so subsequent per-token _vwrite calls
    # don't race with each other. The matching `done`/`stopped`/`error`
    # exits streaming mode and re-locks the view for input.
    if t in ("text", "text_delta", "thinking", "thinking_delta", "tool_use", "tool_result"):
        if view.id() not in _streaming_views:
            _streaming_views.add(view.id())
            sublime.set_timeout(lambda: view.set_read_only(False), 0)

    if t == "text":
        _vwrite(view, event.get("text", ""))
    elif t == "text_delta":
        _vwrite(view, event.get("text", ""))
    elif t == "thinking":
        # Complete (non-streaming) thinking block. The trailing newline
        # ends the line. Only the first one in a burst needs the marker.
        if view.id() in _thinking_started:
            _vwrite(view, event.get("text", "") + "\n")
        else:
            _thinking_started.add(view.id())
            _vwrite(view, f"  💭 {event.get('text', '')}\n")
    elif t == "thinking_delta":
        # Streamed thinking. Only the first delta gets the marker; later
        # deltas just continue the line. No trailing newline until done.
        if view.id() in _thinking_started:
            _vwrite(view, event.get("text", ""))
        else:
            _thinking_started.add(view.id())
            _vwrite(view, f"  💭 {event.get('text', '')}")
    elif t == "tool_use":
        _render_tool_start(
            view,
            event.get("name", "?"),
            event.get("tool_id", ""),
            event.get("input", {}),
        )
    elif t == "tool_approval_request":
        _prompt_approval(
            window,
            event.get("tool_id", ""),
            event.get("name", "?"),
            event.get("input", {}),
        )
    elif t == "tool_result":
        _render_tool_result(
            view,
            event.get("tool_id", ""),
            event.get("is_error", False),
            event.get("result"),
        )
        if event.get("rejected"):
            _vwrite(view, "  [rejected]\n")
    elif t == "done":
        # Make sure the meta + error/stop text land in a writable view even
        # if no content events preceded this (edge case: empty response).
        _streaming_views.add(view.id())
        if event.get("stop_reason") == "interrupted":
            _vwrite(view, "\nInterrupted\n")
        else:
            _render_meta(view, event)
        _update_ccstatus(view, event)
        # Exit streaming: re-lock the view, then enter input mode.
        _streaming_views.discard(view.id())
        _thinking_started.discard(view.id())

        def _lock_and_enter():
            view.set_read_only(True)
            _enter_input_mode(view, window)

        sublime.set_timeout(lambda: _stop_spinner(window), 0)
        sublime.set_timeout(_lock_and_enter, 50)
    elif t == "stopped":
        _streaming_views.add(view.id())
        _vwrite(view, "\n[Stopped]\n")
        _update_ccstatus(view, event)
        _streaming_views.discard(view.id())
        _thinking_started.discard(view.id())

        def _lock_and_enter():
            view.set_read_only(True)
            _enter_input_mode(view, window)

        sublime.set_timeout(lambda: _stop_spinner(window), 0)
        sublime.set_timeout(_lock_and_enter, 50)
    elif t == "error":
        err = event.get("error", "unknown error")
        _streaming_views.add(view.id())
        _vwrite(view, f"\n[Error: {err}]\n")
        _update_ccstatus(view, event)
        _streaming_views.discard(view.id())
        _thinking_started.discard(view.id())

        def _lock_and_enter():
            view.set_read_only(True)
            _enter_input_mode(view, window)

        sublime.set_timeout(lambda: _stop_spinner(window), 0)
        sublime.set_timeout(_lock_and_enter, 50)


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
            if view.settings().get("ai_sdk_view") and not view.settings().get(
                "ai_sdk_input_mode"
            ):
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
    "clear": "Clear the view and reset the conversation",
    "cls": "Clear the view text only (keeps conversation history)",
    "help": "Show available slash commands",
    "status": "Show bridge status",
    "tools": "List tools with on/off state, or /tools on|off <name>",
    "reload-servers": "Reconnect MCP servers without clearing conversation history",
    "compact": "Summarize conversation context (forwarded to SDK)",
    "export-history": "Save conversation to JSON (default ~/.cache/ai_sdk/history_<ts>.json)",
    "import-history": "Load conversation from JSON: /import-history <path>",
    "loop-limit": "Set max tool-loop iterations (default 15): /loop-limit <n>",
    "model": "Switch Ollama model: /model [name] — no arg lists available models",
    "thinking": "Toggle thinking mode (reasoning models): /thinking on|off",
    "prompts": "List MCP prompts, or invoke: /prompts <server:name> arg=value ...",
    "resources": "List MCP resources (read with @<uri>)",
    "hil": "Gate each tool call on approval: /hil on|off",
}


def _handle_slash(view, window, prompt):
    """Return True if handled locally, False to forward to bridge."""
    parts = prompt[1:].split(None, 1)
    cmd = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""

    def _wipe_view():
        """Erase all rendered text. Call on the main thread."""
        view.set_read_only(False)
        view.run_command("select_all")
        view.run_command("left_delete")
        view.set_read_only(True)

    if cmd == "clear":
        # Wipe the view AND reset the backend conversation in place (no
        # subprocess restart). Best-effort clear_history: if the running
        # backend doesn't handle it, the view still clears.
        sublime.set_timeout(_wipe_view, 0)
        if _bridge:
            _bridge.send_request(
                {"id": 1, "type": "clear_history"}, lambda ev: None
            )
        sublime.set_timeout(lambda: _enter_input_mode(view, window), 60)
        return True

    if cmd == "cls":
        # Clear the screen only — keep backend conversation history.
        sublime.set_timeout(_wipe_view, 0)
        sublime.set_timeout(lambda: _enter_input_mode(view, window), 60)
        return True

    if cmd == "help":
        lines = ["", "Slash commands:", ""]
        for name, desc in _SLASH_COMMANDS.items():
            lines.append(f"  /{name:<10} {desc}")
        lines.append("")
        _vwrite(view, "\n".join(lines))
        _enter_input_mode(view, window)
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
        sub_parts = args.split(None, 1) if args else []
        sub = sub_parts[0].lower() if sub_parts else ""
        name = sub_parts[1].strip() if len(sub_parts) > 1 else ""

        if sub in ("on", "off"):
            if not name:
                _vwrite(view, f"\n[Usage: /tools {sub} <tool_name>]\n")
                _enter_input_mode(view, window)
                return True

            def on_set(ev):
                if ev.get("type") == "tool_set":
                    state = "enabled" if ev.get("enabled") else "disabled"
                    _vwrite(view, f"\n[Tool {ev.get('name')} {state}]\n")
                elif ev.get("type") == "error":
                    _vwrite(view, f"\n[Error: {ev.get('error')}]\n")
                sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)

            _bridge.send_request(
                {"id": 1, "type": "set_tool", "name": name, "enabled": sub == "on"},
                on_set,
            )
            return True

        def on_list(ev):
            if ev.get("type") == "tools_list":
                tools = ev.get("tools", [])
                if not tools:
                    _vwrite(view, "\n[No tools loaded — check MCP servers]\n")
                else:
                    by_client = {}
                    for t in tools:
                        by_client.setdefault(t.get("client", "?"), []).append(t)
                    lines = ["", "Tools:", ""]
                    for client, ts in sorted(by_client.items()):
                        lines.append(f"  {client}")
                        for t in sorted(ts, key=lambda x: x["name"]):
                            mark = "✔" if t.get("enabled") else "✘"
                            lines.append(f"    {mark} {t['name']}")
                    lines.append("")
                    lines.append("  use /tools on|off <name> to toggle")
                    _vwrite(view, "\n".join(lines))
            elif ev.get("type") == "error":
                _vwrite(view, f"\n[Error: {ev.get('error')}]\n")
            sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)

        _bridge.send_request({"id": 1, "type": "list_tools"}, on_list)
        return True

    if cmd == "reload-servers":
        def on_reload(ev):
            if ev.get("type") == "servers_reloaded":
                _vwrite(
                    view,
                    f"\n[Servers reloaded — {ev.get('mcp_tools')} MCP tools, "
                    f"{ev.get('total_tools')} total]\n",
                )
            elif ev.get("type") == "error":
                _vwrite(view, f"\n[Reload error: {ev.get('error')}]\n")
            sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)

        _bridge.send_request({"id": 1, "type": "reload_servers"}, on_reload)
        return True

    if cmd == "export-history":
        import time as _time

        path = args.strip()
        if not path:
            _dir = os.path.expanduser("~/.cache/ai_sdk")
            os.makedirs(_dir, exist_ok=True)
            path = os.path.join(
                _dir, f"history_{_time.strftime('%Y%m%d_%H%M%S')}.json"
            )
        path = os.path.expanduser(path)

        def on_export(ev):
            if ev.get("type") == "history_exported":
                _vwrite(view, f"\n[History exported to {ev.get('path')}]\n")
            elif ev.get("type") == "error":
                _vwrite(view, f"\n[Export error: {ev.get('error')}]\n")
            sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)

        _bridge.send_request({"id": 1, "type": "export_history", "path": path}, on_export)
        return True

    if cmd == "import-history":
        path = args.strip()
        if not path:
            _vwrite(view, "\n[Usage: /import-history <path>]\n")
            _enter_input_mode(view, window)
            return True
        path = os.path.expanduser(path)

        def on_import(ev):
            if ev.get("type") == "history_imported":
                _vwrite(
                    view,
                    f"\n[History imported from {ev.get('path')} — "
                    f"{ev.get('turns', 0)} turns]\n",
                )
            elif ev.get("type") == "error":
                _vwrite(view, f"\n[Import error: {ev.get('error')}]\n")
            sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)

        _bridge.send_request({"id": 1, "type": "import_history", "path": path}, on_import)
        return True

    if cmd == "loop-limit":
        arg = args.strip()
        if not arg:
            _vwrite(
                view,
                "\n[Usage: /loop-limit <n> — max tool-loop iterations, "
                "default 15]\n",
            )
            _enter_input_mode(view, window)
            return True
        try:
            n = int(arg)
            if n < 1:
                raise ValueError
        except ValueError:
            _vwrite(view, f"\n[Loop limit must be a positive integer, got {arg!r}]\n")
            _enter_input_mode(view, window)
            return True

        def on_ll(ev):
            if ev.get("type") == "loop_limit_set":
                _vwrite(view, f"\n[Loop limit set to {ev.get('limit')}]\n")
            elif ev.get("type") == "error":
                _vwrite(view, f"\n[Error: {ev.get('error')}]\n")
            sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)

        _bridge.send_request({"id": 1, "type": "set_loop_limit", "limit": n}, on_ll)
        return True

    if cmd == "model":
        arg = args.strip()
        if not arg:
            def on_list(ev):
                if ev.get("type") == "model_list":
                    current = ev.get("current", "")
                    names = ev.get("models", [])
                    if not names:
                        _vwrite(
                            view,
                            "\n[No Ollama models installed — "
                            "pull one with `ollama pull <name>`]\n",
                        )
                    else:
                        lines = ["", "Available Ollama models:", ""]
                        for m in names:
                            mark = " *" if m == current else "  "
                            lines.append(f"{mark} {m}")
                        lines.append(f"\n  current: {current}")
                        lines.append("  use /model <name> to switch")
                        _vwrite(view, "\n".join(lines) + "\n")
                elif ev.get("type") == "error":
                    _vwrite(view, f"\n[Error: {ev.get('error')}]\n")
                sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)

            # First reload model from settings (in case the settings file
            # was edited since the bridge started), then list. This makes
            # /model with no args double as a "reset to settings" action.
            def on_reload_then_list(ev):
                if ev.get("type") == "model_reloaded":
                    if ev.get("source") == "settings":
                        _vwrite(
                            view,
                            f"\n[Model reloaded from settings: "
                            f"{ev.get('model')}]\n",
                        )
                    # fall through to list
                if ev.get("type") == "error":
                    _vwrite(view, f"\n[Error: {ev.get('error')}]\n")
                _bridge.send_request({"id": 1, "type": "list_models"}, on_list)

            _bridge.send_request(
                {"id": 1, "type": "reload_model_from_settings"}, on_reload_then_list
            )
            return True

        def on_switch(ev):
            if ev.get("type") == "model_set":
                _vwrite(view, f"\n[Model switched to {ev.get('model')}]\n")
            elif ev.get("type") == "error":
                _vwrite(view, f"\n[Error: {ev.get('error')}]\n")
            sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)

        _bridge.send_request({"id": 1, "type": "set_model", "model": arg}, on_switch)
        return True

    if cmd == "thinking":
        sub = args.strip().lower()
        if sub == "on":
            enabled = True
        elif sub == "off":
            enabled = False
        else:
            _vwrite(view, "\n[Usage: /thinking on|off — surfaces reasoning for thinking models]\n")
            _enter_input_mode(view, window)
            return True

        def on_thinking(ev):
            if ev.get("type") == "thinking_set":
                state = "on" if ev.get("enabled") else "off"
                _vwrite(view, f"\n[Thinking mode {state}]\n")
            elif ev.get("type") == "error":
                _vwrite(view, f"\n[Error: {ev.get('error')}]\n")
            sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)

        _bridge.send_request(
            {"id": 1, "type": "set_thinking", "enabled": enabled}, on_thinking
        )
        return True

    if cmd == "hil":
        sub = args.strip().lower()
        if sub == "on":
            enabled = True
        elif sub == "off":
            enabled = False
        else:
            _vwrite(
                view,
                "\n[Usage: /hil on|off — gate each tool call on a y/n prompt]\n",
            )
            _enter_input_mode(view, window)
            return True

        def on_hil(ev):
            if ev.get("type") == "hil_set":
                state = "on" if ev.get("enabled") else "off"
                _vwrite(view, f"\n[Human-in-the-loop {state}]\n")
            elif ev.get("type") == "error":
                _vwrite(view, f"\n[Error: {ev.get('error')}]\n")
            sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)

        _bridge.send_request(
            {"id": 1, "type": "set_hil", "enabled": enabled}, on_hil
        )
        return True

    if cmd == "prompts":
        if args:
            parts = args.split(None, 1)
            ref = parts[0]
            arg_str = parts[1] if len(parts) > 1 else ""
            if ":" not in ref:
                _vwrite(view, "\n[Prompt ref must be server:name]\n")
                _enter_input_mode(view, window)
                return True
            server, pname = ref.split(":", 1)
            arguments = {}
            for token in arg_str.split():
                if "=" in token:
                    k, v = token.split("=", 1)
                    arguments[k] = v

            def on_prompt(ev):
                if ev.get("type") == "prompt_result":
                    msgs = ev.get("messages", [])
                    lines = ["", f"◆ prompt {server}:{pname}:", ""]
                    for m in msgs:
                        lines.append(f"  [{m.get('role', '?')}] {m.get('text', '')}")
                    lines.append("")
                    _vwrite(view, "\n".join(lines))
                elif ev.get("type") == "error":
                    _vwrite(view, f"\n[Prompt error: {ev.get('error')}]\n")
                sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)

            _bridge.send_request(
                {
                    "id": 1,
                    "type": "get_prompt",
                    "server": server,
                    "name": pname,
                    "arguments": arguments,
                },
                on_prompt,
            )
            return True

        def on_plist(ev):
            if ev.get("type") == "prompts_list":
                prompts = ev.get("prompts", [])
                if not prompts:
                    _vwrite(view, "\n[No MCP prompts available]\n")
                else:
                    lines = ["", "MCP prompts:", ""]
                    for p in prompts:
                        ref = f"{p['server']}:{p['name']}"
                        if p.get("arguments"):
                            argnames = [
                                a["name"] + ("*" if a.get("required") else "")
                                for a in p["arguments"]
                            ]
                            argstr = f"  ({' '.join(argnames)})" if argnames else ""
                        else:
                            argstr = ""
                        desc = (
                            f"  — {p['description']}" if p.get("description") else ""
                        )
                        lines.append(f"  {ref}{argstr}{desc}")
                    lines.append("")
                    lines.append("  use /prompts <server:name> arg=value ...")
                    _vwrite(view, "\n".join(lines))
            elif ev.get("type") == "error":
                _vwrite(view, f"\n[Error: {ev.get('error')}]\n")
            sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)

        _bridge.send_request({"id": 1, "type": "list_prompts"}, on_plist)
        return True

    if cmd == "resources":
        def on_rlist(ev):
            if ev.get("type") == "resources_list":
                resources = ev.get("resources", [])
                if not resources:
                    _vwrite(view, "\n[No MCP resources available]\n")
                else:
                    lines = ["", "MCP resources:", ""]
                    for r in resources:
                        desc = (
                            f"  — {r['description']}" if r.get("description") else ""
                        )
                        lines.append(f"  @{r['uri']}  ({r['server']}){desc}")
                    lines.append("")
                    lines.append("  use @<uri> to read a resource")
                    _vwrite(view, "\n".join(lines))
            elif ev.get("type") == "error":
                _vwrite(view, f"\n[Error: {ev.get('error')}]\n")
            sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)

        _bridge.send_request({"id": 1, "type": "list_resources"}, on_rlist)
        return True

    # Unknown command — forward to bridge (handles /compact, /loop, etc.)
    return False


def _do_submit(view, window, prompt):
    _exit_input_mode(view)
    _render_prompt(view, prompt)

    if prompt.startswith("@"):
        uri = prompt[1:].strip()
        def on_res(ev):
            if ev.get("type") == "resource_read":
                _vwrite(view, f"\n{ev.get('text', '')}\n")
            elif ev.get("type") == "error":
                _vwrite(view, f"\n[Resource error: {ev.get('error')}]\n")
            sublime.set_timeout(lambda: _enter_input_mode(view, window), 0)
        _bridge.send_request({"id": 1, "type": "read_resource", "uri": uri}, on_res)
        return

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
    """Sidebar: open AI(SDK) panel with cwd set to the chosen directory.

    Always restarts the bridge subprocess so its os.getcwd() matches
    the chosen folder. If we didn't restart, the bridge would keep
    whatever cwd it was originally spawned with (often ST's own cwd
    = the user's home) and 'pwd' inside run_shell would disagree
    with the cwd shown in the view header.
    """

    def run(self, paths=None):
        import os as _os
        import subprocess as _subprocess

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
        # Kill any existing bridge on port 9504 BEFORE starting a new
        # one with the chosen cwd. Without this, the new subprocess
        # would fail to bind (Windows doesn't set SO_REUSEADDR on
        # asyncio.start_server) and the user would be left with the
        # old bridge — running on whatever cwd it had at first start.
        # The user explicitly invoked "Open here..." with the intent
        # of changing the bridge's working directory; honour that.
        _subprocess.run(
            [
                "powershell", "-Command",
                f"Get-NetTCPConnection -LocalPort {_BRIDGE_PORT} "
                f"-State Listen -ErrorAction SilentlyContinue | "
                f"ForEach-Object {{ Stop-Process -Id $_.OwningProcess "
                f"-Force -ErrorAction SilentlyContinue }}",
            ],
            capture_output=True,
        )
        # Wait for the OS to release the port. On Windows this can
        # take a couple of seconds — poll until the port is actually
        # free, with a hard cap of 8s.
        import time as _time
        import socket as _socket
        deadline = _time.time() + 8.0
        while _time.time() < deadline:
            _time.sleep(0.3)
            probe = _socket.socket()
            probe.settimeout(0.2)
            try:
                probe.connect(("127.0.0.1", _BRIDGE_PORT))
                probe.close()
                # port still listening — keep waiting
                continue
            except Exception:
                # port is free
                break
            finally:
                try: probe.close()
                except Exception: pass
        # Drop our wrapper so _ensure_bridge spawns a fresh subprocess
        # with the new cwd, not reuses the old (port-in-use) listener.
        _bridge = None
        _bridge_cwd = path
        _ensure_bridge()
        view = _sdk_view(self.window)
        self.window.focus_view(view)
        if view.settings().get("ai_sdk_input_mode", False):
            _exit_input_mode(view)
        _vwrite(view, f"\n─── cwd: {path} ───\n")
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
