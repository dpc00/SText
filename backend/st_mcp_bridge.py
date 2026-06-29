"""st_mcp_bridge.py — stdio MCP bridge to the ai_sdk.py socket server in ST.

Usage: python st_mcp_bridge.py
Registers as a stdio MCP server; routes tool calls to ST via TCP 127.0.0.1:9503.
"""

import json
import socket
import sys

_HOST = "127.0.0.1"
_PORT = 9503

_TOOLS = [
    {
        "name": "get_window_summary",
        "description": (
            "Get current Sublime Text editor state: active file + cursor position, "
            "open files, project folder. Fast single-call snapshot."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sublime_eval",
        "description": (
            "Execute Python code directly in Sublime Text's context. "
            "Available globals: sublime, sublime_plugin, os. "
            "Use 'return <value>' to return a result. "
            "Use for anything not covered by other tools."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"}
            },
            "required": ["code"],
        },
    },
]

_WINDOW_SUMMARY_CODE = """
window = sublime.active_window()
if not window:
    return "No window"
lines = []
folders = window.folders()
if folders:
    lines.append("Project: " + folders[0])
active = window.active_view()
if active and active.file_name():
    sel = active.sel()
    row, col = active.rowcol(sel[0].begin()) if sel else (0, 0)
    lines.append("Active: " + active.file_name() + ":" + str(row + 1) + ":" + str(col + 1))
elif active and active.name():
    lines.append("Active (scratch): " + active.name())
open_files = [v.file_name() for v in window.views() if v.file_name()]
lines.append("Open (" + str(len(open_files)) + "):")
for f in open_files[:20]:
    lines.append("  " + os.path.basename(f))
if len(open_files) > 20:
    lines.append("  ... and " + str(len(open_files) - 20) + " more")
return "\\n".join(lines)
"""


def _log(msg):
    sys.stderr.write(f"[st_mcp_bridge] {msg}\n")
    sys.stderr.flush()


def _send_to_st(code):
    preview = code.strip()[:60].replace("\n", "↵")
    _log(f"→ ST eval: {preview!r}")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((_HOST, _PORT))
        sock.sendall((json.dumps({"code": code}) + "\n").encode())
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        sock.close()
        result = json.loads(data.strip())
        if result.get("error"):
            _log(f"← error: {result['error']}")
        else:
            val = result.get("result")
            preview_out = repr(val)[:60] if val is not None else "None"
            _log(f"← result: {preview_out}")
        return result
    except ConnectionRefusedError:
        _log(f"← connection refused on port {_PORT}")
        return {"error": f"ST not connected (port {_PORT} refused — is ai_sdk.py loaded in ST?)"}
    except Exception as e:
        _log(f"← exception: {e}")
        return {"error": str(e)}


def _ok(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_, msg):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": -32000, "message": msg}}


def _handle(req):
    id_ = req.get("id")
    method = req.get("method", "")
    params = req.get("params", {})

    if method == "initialize":
        return _ok(id_, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "st-plugin", "version": "0.1.0"},
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return _ok(id_, {"tools": _TOOLS})

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        _log(f"tool call: {name}")

        if name == "get_window_summary":
            result = _send_to_st(_WINDOW_SUMMARY_CODE)
        elif name == "sublime_eval":
            result = _send_to_st(args.get("code", ""))
        else:
            _log(f"unknown tool: {name}")
            return _err(id_, f"Unknown tool: {name}")

        if result.get("error"):
            return _ok(id_, {
                "content": [{"type": "text", "text": f"Error: {result['error']}"}],
                "isError": True,
            })

        val = result.get("result")
        if val is None:
            text = "(no return value)"
        elif isinstance(val, str):
            text = val
        else:
            text = json.dumps(val, indent=2)
        return _ok(id_, {"content": [{"type": "text", "text": text}]})

    return None


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = _handle(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
        except Exception as e:
            sys.stderr.write(f"[st_mcp_bridge] {e}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    main()
