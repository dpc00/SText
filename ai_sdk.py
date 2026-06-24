"""ai_sdk.py — Claude via Agent SDK, no terminal required.

AiSdkQueryCommand  Ctrl+Alt+A → input panel → Claude answers in "Ai (SDK)" view
Socket server      127.0.0.1:9503 — MCP bridge sends tool calls here for ST eval
"""

import json
import os
import socket
import subprocess
import threading

import sublime  # type: ignore
import sublime_plugin  # type: ignore

_VIEW_NAME = "Ai (SDK)"
_DIVIDER = "─" * 60
_PYTHON = r"C:\Users\donal\AppData\Local\Programs\Python\Python312\python.exe"
_SCRIPT = r"C:\Users\donal\projects\tools\agent_query.py"

_SOCKET_HOST = "127.0.0.1"
_SOCKET_PORT = 9503


# ─── Socket server ────────────────────────────────────────────────────────────

def _eval_in_st(code):
    """Execute Python code in ST's context and return the result."""
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
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

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
            print(f"[ai_sdk] socket server on {_SOCKET_HOST}:{_SOCKET_PORT}")
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
            print(f"[ai_sdk] socket server error: {e}")

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
            preview = code.strip()[:60].replace("\n", "↵")
            print(f"[ai_sdk] eval: {preview!r}")
            result = {"result": None, "error": None}
            done = threading.Event()

            def do_eval():
                try:
                    result["result"] = _eval_in_st(code)
                    val = result["result"]
                    preview_out = repr(val)[:60] if val is not None else "None"
                    print(f"[ai_sdk] result: {preview_out}")
                except Exception as exc:
                    result["error"] = str(exc)
                    print(f"[ai_sdk] error: {exc}")
                finally:
                    done.set()

            sublime.set_timeout(do_eval, 0)
            timed_out = not done.wait(timeout=10.0)
            if timed_out:
                result["error"] = "eval timed out after 10s"
                print(f"[ai_sdk] TIMEOUT for: {preview!r}")
            conn.sendall((json.dumps(result) + "\n").encode())
        except Exception as e:
            print(f"[ai_sdk] handle error: {e}")
            try:
                conn.sendall((json.dumps({"error": str(e)}) + "\n").encode())
            except Exception:
                pass
        finally:
            conn.close()


_server = None


def plugin_loaded():
    global _server
    _server = _SdkSocketServer()
    _server.start()


def plugin_unloaded():
    global _server
    if _server:
        _server.stop()
        _server = None


# ─── Query view helpers ───────────────────────────────────────────────────────

def _sdk_view(window):
    for v in window.views():
        if v.name() == _VIEW_NAME:
            return v
    v = window.new_file()
    v.set_name(_VIEW_NAME)
    v.set_scratch(True)
    v.settings().set("word_wrap", True)
    v.settings().set("gutter", False)
    v.settings().set("line_numbers", False)
    v.settings().set("ai_sdk_view", True)
    return v


def _append(view, text):
    def _do():
        view.set_read_only(False)
        view.run_command("append", {"characters": text, "scroll_to_end": True})
        view.set_read_only(True)
    sublime.set_timeout(_do, 0)


def _run(prompt, view, cwd=None):
    try:
        proc = subprocess.Popen(
            [_PYTHON, _SCRIPT, prompt],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            cwd=cwd,
        )
        for chunk in iter(lambda: proc.stdout.read(256), ""):
            _append(view, chunk)
        proc.wait()
        if proc.returncode != 0:
            err = proc.stderr.read()
            if err:
                _append(view, f"\n[Error: {err.strip()}]\n")
    except Exception as e:
        _append(view, f"\n[Error: {e}]\n")
    finally:
        _append(view, "\n")


# ─── Command ──────────────────────────────────────────────────────────────────

class AiSdkQueryCommand(sublime_plugin.WindowCommand):
    """Ctrl+Alt+A — ask Claude; response streams into the Ai (SDK) view."""

    def run(self):
        self.window.show_input_panel(
            "Ask Claude:", "", self._submit, None, None
        )

    def _resolve_cwd(self):
        view = self.window.active_view()
        if view and view.file_name():
            return os.path.dirname(view.file_name())
        folders = self.window.folders()
        return folders[0] if folders else os.path.expanduser("~")

    def _submit(self, prompt):
        prompt = prompt.strip()
        if not prompt:
            return
        cwd = self._resolve_cwd()
        view = _sdk_view(self.window)
        _append(view, f"\n{_DIVIDER}\nYou: {prompt}\n{_DIVIDER}\n↺ thinking…\n")
        threading.Thread(target=_run, args=(prompt, view, cwd), daemon=True).start()
