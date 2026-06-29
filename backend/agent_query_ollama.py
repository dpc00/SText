"""agent_query_ollama.py — Stream an Ollama response to stdout, with MCP tool support.

Drop-in replacement for agent_query.py that uses Ollama instead of Claude Agent SDK.
Speaks the same JSON-lines TCP bridge protocol as the original.

Usage:
  python agent_query_ollama.py "your prompt here"     # one-shot
  python agent_query_ollama.py --bridge PORT           # persistent multi-turn bridge
"""

import asyncio
import json
import os
import sys
import subprocess
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass  # ST's _LogWriter doesn't support reconfigure

_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
_PYTHON = r"C:\Users\donal\AppData\Local\Programs\Python\Python312\python.exe"

# ─── MCP server definitions (same as original) ───────────────────────────────


def _load_mcp_servers():
    """Load mcpServers from ~/.ollama/config.json."""
    servers = {}
    try:
        cfg = json.load(
            open(os.path.expanduser("~/.ollama/config.json"), encoding="utf-8")
        )
        servers.update(cfg.get("mcpServers", {}))
    except Exception:
        pass
    return servers


# ─── Context files ───────────────────────────────────────────────────────────

_CONTEXT_FILES = [
    r"C:\Users\donal\agents.md",
    r"C:\Users\donal\router.md",
    r"C:\Users\donal\projects\SText\CLAUDE.md",
]


def _build_system_prompt():
    parts = []
    for path in _CONTEXT_FILES:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                parts.append(f"# {os.path.basename(path)}\n{f.read()}")
        except OSError:
            pass
    context = "\n\n---\n\n".join(parts)
    return f"""You are an AI coding assistant running inside Sublime Text via the ai_sdk plugin.
You have access to MCP tools for Sublime Text (sublime-mcp), screenshots, web scraping (firecrawl), and more.
Use tools when they help. Be concise and direct.

{context}"""


# ─── MCP tool loading ────────────────────────────────────────────────────────


class StdioMCPClient:
    """Minimal stdio MCP client: launches a subprocess, speaks JSON-RPC over stdin/stdout."""

    def __init__(self, name, command, args, env=None):
        self.name = name
        self.proc = None
        self._id = 0
        self._lock = threading.Lock()
        self.command = command
        self.args = args
        self.env = env
        self.tools = []
        self._initialized = False

    def start(self):
        full_env = {**os.environ, **(self.env or {})}
        self.proc = subprocess.Popen(
            [self.command] + self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=full_env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        # Initialize
        resp = self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agent_query_ollama", "version": "0.1"},
            },
        )
        if resp:
            self._initialized = True
            # Send initialized notification
            self._notify("notifications/initialized", {})
            # List tools
            tools_resp = self._rpc("tools/list", {})
            if tools_resp and "tools" in tools_resp:
                self.tools = tools_resp["tools"]
        return self._initialized

    def call_tool(self, name, arguments):
        resp = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if resp and "content" in resp:
            # MCP returns content as list of {type, text} blocks
            texts = []
            for block in resp["content"]:
                if block.get("type") == "text":
                    texts.append(block["text"])
            return "\n".join(texts)
        return json.dumps(resp) if resp else "Error: no response"

    def stop(self):
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None

    def _rpc(self, method, params):
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        with self._lock:
            try:
                self.proc.stdin.write(json.dumps(req) + "\n")
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
                if line:
                    resp = json.loads(line)
                    return resp.get("result")
            except Exception as e:
                print(f"[mcp:{self.name}] rpc error: {e}", file=sys.stderr)
        return None

    def _notify(self, method, params):
        req = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass


class SSEMCPClient:
    """HTTP MCP client for sublime-mcp. Uses the HTTP bridge on port 9500
    with per-tool endpoints (GET or POST). Dynamically fetches the tool list."""

    def __init__(self, name, url):
        self.name = name
        # SSE url is http://127.0.0.1:9502/sse — HTTP bridge is on port 9500
        self.sse_url = url
        self.base_url = "http://127.0.0.1:9500"
        self.tools = []

    def start(self):
        import urllib.request

        try:
            # Fetch the tool list by calling the SSE server's tools/list via a
            # simple approach: GET /tools_list on the HTTP bridge.
            # The HTTP bridge doesn't have a tools/list endpoint, so we build
            # the tool list from the known _MCP_TOOLS route table in sublime_mcp.py.
            # Each route maps to a tool name. GET routes = read tools, POST routes = write tools.
            self.tools = self._discover_tools()
            print(
                f"[mcp:{self.name}] {len(self.tools)} tools discovered", file=sys.stderr
            )
            return len(self.tools) > 0
        except Exception as e:
            print(f"[mcp:{self.name}] start error: {e}", file=sys.stderr)
            return False

    def _discover_tools(self):
        """Discover tools by probing the HTTP bridge endpoints."""
        import urllib.request

        # Probe a known endpoint to confirm the server is up
        try:
            req = urllib.request.Request(f"{self.base_url}/active_file")
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass  # server is alive
        except Exception:
            raise RuntimeError(
                f"Cannot connect to sublime-mcp HTTP bridge at {self.base_url}"
            )

        # Build tool list from the known route table.
        # Each tuple: (tool_name, description, http_method, endpoint, input_schema)
        routes = [
            # GET (read-only) tools
            (
                "get_active_file",
                "Get the active file's path, content, cursor position, dirty flag, and syntax name.",
                "GET",
                "/active_file",
                {"type": "object", "properties": {}},
            ),
            (
                "get_selection",
                "Return the current selection(s).",
                "GET",
                "/selection",
                {"type": "object", "properties": {}},
            ),
            (
                "get_cursor_context",
                "Get lines around the cursor.",
                "GET",
                "/cursor_context",
                {"type": "object", "properties": {"lines": {"type": "integer"}}},
            ),
            (
                "get_open_files",
                "List all open files.",
                "GET",
                "/open_files",
                {"type": "object", "properties": {}},
            ),
            (
                "get_sheets",
                "List all sheets (tabs).",
                "GET",
                "/sheets",
                {"type": "object", "properties": {}},
            ),
            (
                "get_sheet_content",
                "Get content of a sheet by index.",
                "GET",
                "/sheet_content",
                {
                    "type": "object",
                    "properties": {"index": {"type": "integer"}},
                    "required": ["index"],
                },
            ),
            (
                "get_project_folders",
                "Get project folder paths.",
                "GET",
                "/project_folders",
                {"type": "object", "properties": {}},
            ),
            (
                "get_file_content",
                "Get content of an open file by path.",
                "GET",
                "/file_content",
                {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            ),
            (
                "get_view_content",
                "Get content of a view by name.",
                "GET",
                "/view_content",
                {"type": "object", "properties": {"name": {"type": "string"}}},
            ),
            (
                "get_view_size",
                "Get total character count of a view.",
                "GET",
                "/view_size",
                {"type": "object", "properties": {"name": {"type": "string"}}},
            ),
            (
                "get_view_chars",
                "Get text at character offsets.",
                "GET",
                "/view_chars",
                {
                    "type": "object",
                    "properties": {
                        "begin": {"type": "integer"},
                        "end": {"type": "integer"},
                        "name": {"type": "string"},
                    },
                    "required": ["begin", "end"],
                },
            ),
            (
                "get_view_phantoms",
                "Get phantom HTML/text from a view.",
                "GET",
                "/view_phantoms",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "key": {"type": "string"},
                    },
                },
            ),
            (
                "get_output_panel",
                "Get text content of an output panel.",
                "GET",
                "/output_panel",
                {"type": "object", "properties": {"name": {"type": "string"}}},
            ),
            (
                "get_console_log",
                "Get recent ST console output.",
                "GET",
                "/console_log",
                {"type": "object", "properties": {"tail": {"type": "integer"}}},
            ),
            (
                "get_console_full",
                "Get the entire captured ST console buffer.",
                "GET",
                "/console_full",
                {"type": "object", "properties": {}},
            ),
            (
                "get_console_win",
                "Windows-only: captures ST console via ctypes.",
                "GET",
                "/console_win",
                {"type": "object", "properties": {}},
            ),
            (
                "get_symbols",
                "Get all symbols in the active file.",
                "GET",
                "/symbols",
                {"type": "object", "properties": {}},
            ),
            (
                "lookup_symbol",
                "Find where a symbol is defined across all open files.",
                "GET",
                "/lookup_symbol",
                {
                    "type": "object",
                    "properties": {"symbol": {"type": "string"}},
                    "required": ["symbol"],
                },
            ),
            (
                "get_project_data",
                "Get raw .sublime-project JSON data.",
                "GET",
                "/project_data",
                {"type": "object", "properties": {}},
            ),
            (
                "get_variables",
                "Get Sublime Text build variables.",
                "GET",
                "/variables",
                {"type": "object", "properties": {}},
            ),
            (
                "get_bookmarks",
                "Get all bookmarked positions in the active file.",
                "GET",
                "/bookmarks",
                {"type": "object", "properties": {}},
            ),
            (
                "get_line_count",
                "Get total number of lines in the active file.",
                "GET",
                "/line_count",
                {"type": "object", "properties": {}},
            ),
            (
                "get_syntaxes",
                "List all syntax definitions.",
                "GET",
                "/syntaxes",
                {"type": "object", "properties": {}},
            ),
            (
                "get_command_palette",
                "List Command Palette entries.",
                "GET",
                "/command_palette",
                {
                    "type": "object",
                    "properties": {
                        "package": {"type": "string"},
                        "command": {"type": "string"},
                        "caption": {"type": "string"},
                    },
                },
            ),
            (
                "get_commands",
                "List runnable Sublime commands.",
                "GET",
                "/commands",
                {
                    "type": "object",
                    "properties": {
                        "package": {"type": "string"},
                        "command": {"type": "string"},
                        "include_palette": {"type": "boolean"},
                    },
                },
            ),
            (
                "get_menu_items",
                "List installed menu items.",
                "GET",
                "/menu_items",
                {
                    "type": "object",
                    "properties": {
                        "menu": {"type": "string"},
                        "caption": {"type": "string"},
                        "command": {"type": "string"},
                    },
                },
            ),
            (
                "get_active_panel",
                "Get the active panel id and content.",
                "GET",
                "/active_panel",
                {"type": "object", "properties": {}},
            ),
            (
                "get_scope_at_cursor",
                "Get syntax scope at cursor.",
                "GET",
                "/scope_at_cursor",
                {"type": "object", "properties": {}},
            ),
            (
                "get_encoding",
                "Get character encoding of the active file.",
                "GET",
                "/encoding",
                {"type": "object", "properties": {}},
            ),
            (
                "get_word_at_cursor",
                "Get word under cursor.",
                "GET",
                "/word_at_cursor",
                {"type": "object", "properties": {}},
            ),
            (
                "get_layout",
                "Get current window layout.",
                "GET",
                "/layout",
                {"type": "object", "properties": {}},
            ),
            (
                "get_setting",
                "Get a Sublime Text setting.",
                "GET",
                "/get_setting",
                {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "scope": {"type": "string"},
                    },
                    "required": ["key"],
                },
            ),
            (
                "get_package_mcp_info",
                "Get info needed to write an MCP extension for a package.",
                "POST",
                "/package_mcp_info",
                {
                    "type": "object",
                    "properties": {"package": {"type": "string"}},
                    "required": ["package"],
                },
            ),
            (
                "search_packages",
                "Search Package Control for installable packages.",
                "POST",
                "/search_packages",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                },
            ),
            (
                "install_package",
                "Install a Package Control package.",
                "POST",
                "/install_package",
                {
                    "type": "object",
                    "properties": {"package": {"type": "string"}},
                    "required": ["package"],
                },
            ),
            # POST (write) tools
            (
                "str_replace_based_edit_tool",
                "Edit a file: str_replace, insert, create, or view.",
                "POST",
                "/edit_file",
                {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "path": {"type": "string"},
                        "old_str": {"type": "string"},
                        "new_str": {"type": "string"},
                        "insert_line": {"type": "integer"},
                        "insert_text": {"type": "string"},
                        "file_text": {"type": "string"},
                        "view_range": {"type": "array", "items": {"type": "integer"}},
                    },
                    "required": ["command"],
                },
            ),
            (
                "open_file",
                "Open a file, optionally at a line and column.",
                "POST",
                "/open_file",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "line": {"type": "integer"},
                        "col": {"type": "integer"},
                    },
                    "required": ["path"],
                },
            ),
            (
                "goto_line",
                "Move cursor to a line in the active file.",
                "POST",
                "/goto_line",
                {
                    "type": "object",
                    "properties": {
                        "line": {"type": "integer"},
                        "col": {"type": "integer"},
                    },
                    "required": ["line"],
                },
            ),
            (
                "show_panel",
                "Bring an output panel to the front.",
                "POST",
                "/show_panel",
                {"type": "object", "properties": {"name": {"type": "string"}}},
            ),
            (
                "replace_selection",
                "Replace the current selection(s) with text.",
                "POST",
                "/replace_selection",
                {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            ),
            (
                "replace_lines",
                "Replace lines begin through end with text.",
                "POST",
                "/replace_lines",
                {
                    "type": "object",
                    "properties": {
                        "begin": {"type": "integer"},
                        "end": {"type": "integer"},
                        "text": {"type": "string"},
                        "path": {"type": "string"},
                        "index": {"type": "integer"},
                    },
                    "required": ["begin", "end", "text"],
                },
            ),
            (
                "run_command",
                "Run any Sublime Text command.",
                "POST",
                "/run_command",
                {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "args": {},
                        "scope": {"type": "string"},
                    },
                    "required": ["command"],
                },
            ),
            (
                "run_build",
                "Trigger the current build system or run a specific command.",
                "POST",
                "/run_build",
                {
                    "type": "object",
                    "properties": {
                        "cmd": {"type": "array", "items": {"type": "string"}},
                        "shell_cmd": {"type": "string"},
                        "working_dir": {"type": "string"},
                    },
                },
            ),
            (
                "set_status",
                "Write a message to ST's status bar.",
                "POST",
                "/set_status",
                {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "key": {"type": "string"},
                    },
                    "required": ["value"],
                },
            ),
            (
                "save_file",
                "Save a file by path, or the active file.",
                "POST",
                "/save_file",
                {"type": "object", "properties": {"path": {"type": "string"}}},
            ),
            (
                "save_all",
                "Save all open files.",
                "POST",
                "/save_all",
                {"type": "object", "properties": {}},
            ),
            (
                "close_file",
                "Close a file by path, or the active file.",
                "POST",
                "/close_file",
                {"type": "object", "properties": {"path": {"type": "string"}}},
            ),
            (
                "find_in_files",
                "Search for pattern across project folders.",
                "POST",
                "/find_in_files",
                {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "folders": {"type": "array", "items": {"type": "string"}},
                        "case_sensitive": {"type": "boolean"},
                        "regex": {"type": "boolean"},
                        "max_results": {"type": "integer"},
                    },
                    "required": ["pattern"],
                },
            ),
            (
                "find_in_file",
                "Find all occurrences of pattern in active file.",
                "POST",
                "/find_in_file",
                {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "case_sensitive": {"type": "boolean"},
                        "regex": {"type": "boolean"},
                    },
                    "required": ["pattern"],
                },
            ),
            (
                "set_syntax",
                "Set the syntax of the active file.",
                "POST",
                "/set_syntax",
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            ),
            (
                "set_encoding",
                "Set character encoding of the active file.",
                "POST",
                "/set_encoding",
                {
                    "type": "object",
                    "properties": {"encoding": {"type": "string"}},
                    "required": ["encoding"],
                },
            ),
            (
                "toggle_comment",
                "Toggle line or block comment on selection.",
                "POST",
                "/toggle_comment",
                {"type": "object", "properties": {"block": {"type": "boolean"}}},
            ),
            (
                "toggle_sidebar",
                "Show or hide the sidebar.",
                "POST",
                "/toggle_sidebar",
                {"type": "object", "properties": {}},
            ),
            (
                "select_lines",
                "Select lines begin through end.",
                "POST",
                "/select_lines",
                {
                    "type": "object",
                    "properties": {
                        "begin": {"type": "integer"},
                        "end": {"type": "integer"},
                    },
                    "required": ["begin"],
                },
            ),
            (
                "sort_lines",
                "Sort selected lines or all lines.",
                "POST",
                "/sort_lines",
                {
                    "type": "object",
                    "properties": {"case_sensitive": {"type": "boolean"}},
                },
            ),
            (
                "eval_python",
                "Execute Python in ST's main thread.",
                "POST",
                "/eval_python",
                {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            ),
            (
                "eval_python_latest",
                "Execute Python using system Python interpreter.",
                "POST",
                "/eval_python_latest",
                {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            ),
            (
                "fold_lines",
                "Fold lines begin through end.",
                "POST",
                "/fold_lines",
                {
                    "type": "object",
                    "properties": {
                        "begin": {"type": "integer"},
                        "end": {"type": "integer"},
                    },
                    "required": ["begin", "end"],
                },
            ),
            (
                "insert_snippet",
                "Insert a snippet at the cursor.",
                "POST",
                "/insert_snippet",
                {
                    "type": "object",
                    "properties": {"contents": {"type": "string"}},
                    "required": ["contents"],
                },
            ),
            (
                "revert_file",
                "Revert active file to last saved state.",
                "POST",
                "/revert_file",
                {"type": "object", "properties": {}},
            ),
            (
                "undo",
                "Undo the last edit in the active file.",
                "POST",
                "/undo",
                {"type": "object", "properties": {}},
            ),
            (
                "redo",
                "Redo the last undone edit.",
                "POST",
                "/redo",
                {"type": "object", "properties": {}},
            ),
            (
                "duplicate_line",
                "Duplicate the current line(s).",
                "POST",
                "/duplicate_line",
                {"type": "object", "properties": {}},
            ),
            (
                "set_setting",
                "Set a Sublime Text setting.",
                "POST",
                "/set_setting",
                {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": "string"},
                        "scope": {"type": "string"},
                    },
                    "required": ["key", "value"],
                },
            ),
            (
                "set_project_data",
                "Set raw .sublime-project JSON data.",
                "POST",
                "/set_project_data",
                {"type": "object", "properties": {}},
            ),
            (
                "focus_group",
                "Move focus to a pane group by index.",
                "POST",
                "/focus_group",
                {
                    "type": "object",
                    "properties": {"group": {"type": "integer"}},
                    "required": ["group"],
                },
            ),
            (
                "set_layout",
                "Set window pane layout.",
                "POST",
                "/set_layout",
                {
                    "type": "object",
                    "properties": {"layout": {}},
                    "required": ["layout"],
                },
            ),
            (
                "send_to_view",
                "Send text to an open tab by name.",
                "POST",
                "/send_to_view",
                {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "name": {"type": "string"},
                        "index": {"type": "integer"},
                    },
                    "required": ["text"],
                },
            ),
            (
                "open_control_panel",
                "Open the Claude MCP Control Panel.",
                "POST",
                "/open_control_panel",
                {"type": "object", "properties": {}},
            ),
            (
                "get_help",
                "Return the Agent Guide with instructions on using sublime-mcp tools.",
                "GET",
                "/get_help",
                {"type": "object", "properties": {}},
            ),
        ]
        tools = []
        for name, desc, method, endpoint, schema in routes:
            tools.append(
                {
                    "name": name,
                    "description": desc,
                    "inputSchema": schema,
                    "_method": method,
                    "_endpoint": endpoint,
                }
            )
        return tools

    def call_tool(self, name, arguments):
        import urllib.request

        tool = None
        for t in self.tools:
            if t["name"] == name:
                tool = t
                break
        if not tool:
            return f"Error: tool {name} not found"

        url = f"{self.base_url}{tool['_endpoint']}"
        try:
            if tool["_method"] == "GET":
                # Add query params for GET
                if arguments:
                    import urllib.parse

                    qs = urllib.parse.urlencode(
                        {
                            k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                            for k, v in arguments.items()
                        }
                    )
                    url = f"{url}?{qs}"
                req = urllib.request.Request(url)
            else:
                payload = json.dumps(arguments).encode()
                req = urllib.request.Request(
                    url, data=payload, headers={"Content-Type": "application/json"}
                )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read().decode()
                try:
                    result = json.loads(data)
                    if isinstance(result, dict):
                        # Return the content directly
                        return json.dumps(result)
                    return str(result)
                except json.JSONDecodeError:
                    return data
        except Exception as e:
            return f"Error calling {name}: {e}"

    def stop(self):
        pass


# Known sublime-mcp tools (from the /status output we saw earlier)
_SUBLIME_MCP_TOOLS = [
    {
        "name": "get_active_file",
        "description": "Get the active file's path, content, cursor position, dirty flag, and syntax name.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_selection",
        "description": "Return the current selection(s).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_open_files",
        "description": "List all open files.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_sheets",
        "description": "List all sheets (tabs).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_sheet_content",
        "description": "Get content of a sheet by index.",
        "inputSchema": {
            "type": "object",
            "properties": {"index": {"type": "integer"}},
            "required": ["index"],
        },
    },
    {
        "name": "get_project_folders",
        "description": "Get project folder paths.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_file_content",
        "description": "Get content of an open file by path.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "get_view_content",
        "description": "Get content of a view by name.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
    },
    {
        "name": "find_in_files",
        "description": "Search for pattern across project folders.",
        "inputSchema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}, "regex": {"type": "boolean"}},
            "required": ["pattern"],
        },
    },
    {
        "name": "find_in_file",
        "description": "Find all occurrences of pattern in active file.",
        "inputSchema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}, "regex": {"type": "boolean"}},
            "required": ["pattern"],
        },
    },
    {
        "name": "get_symbols",
        "description": "Get all symbols in the active file.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_cursor_context",
        "description": "Get lines around the cursor.",
        "inputSchema": {"type": "object", "properties": {"lines": {"type": "integer"}}},
    },
    {
        "name": "open_file",
        "description": "Open a file, optionally at a line.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "line": {"type": "integer"},
                "col": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_console_log",
        "description": "Get recent ST console output.",
        "inputSchema": {"type": "object", "properties": {"tail": {"type": "integer"}}},
    },
    {
        "name": "get_commands",
        "description": "List runnable Sublime commands.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_command_palette",
        "description": "List Command Palette entries.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_layout",
        "description": "Get current window layout.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_word_at_cursor",
        "description": "Get word under cursor.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_scope_at_cursor",
        "description": "Get syntax scope at cursor.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_setting",
        "description": "Get a Sublime Text setting.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "scope": {"type": "string"}},
            "required": ["key"],
        },
    },
]


# ─── Tool manager ────────────────────────────────────────────────────────────


class ToolManager:
    """Manages all MCP clients and provides unified tool list + execution."""

    def __init__(self, server_configs):
        self.clients = {}
        self.tools = []  # [{name, description, inputSchema, _client, _orig_name}]

        for name, cfg in server_configs.items():
            stype = cfg.get("type", "stdio")
            # st-plugin needs ai_sdk.py socket server (not running in ollama mode)
            if name == "st-plugin":
                continue
            if stype == "stdio":
                client = StdioMCPClient(
                    name, cfg["command"], cfg.get("args", []), cfg.get("env")
                )
            elif stype == "sse":
                client = SSEMCPClient(name, cfg["url"])
            else:
                print(
                    f"[mcp] unknown type {stype} for {name}, skipping", file=sys.stderr
                )
                continue

            try:
                if client.start():
                    self.clients[name] = client
                    for tool in client.tools:
                        tname = f"mcp__{name}__{tool['name']}"
                        self.tools.append(
                            {
                                "type": "function",
                                "function": {
                                    "name": tname,
                                    "description": tool.get("description", ""),
                                    "parameters": tool.get(
                                        "inputSchema",
                                        {"type": "object", "properties": {}},
                                    ),
                                },
                                "_client": name,
                                "_orig_name": tool["name"],
                            }
                        )
                    print(
                        f"[mcp] {name}: {len(client.tools)} tools loaded",
                        file=sys.stderr,
                    )
                else:
                    print(f"[mcp] {name}: failed to start, skipping", file=sys.stderr)
            except Exception as e:
                print(f"[mcp] {name}: error: {e}, skipping", file=sys.stderr)

    def call(self, full_name, arguments):
        # Find the tool
        for t in self.tools:
            if t["function"]["name"] == full_name:
                client = self.clients[t["_client"]]
                return client.call_tool(t["_orig_name"], arguments)
        return f"Error: tool {full_name} not found"

    def stop(self):
        for c in self.clients.values():
            c.stop()


# ─── Ollama bridge ───────────────────────────────────────────────────────────


async def main_bridge(port: int):
    """Persistent bridge: multi-turn Ollama chat, listens on TCP port."""
    import ollama

    server_configs = _load_mcp_servers()
    tm = ToolManager(server_configs)
    print(
        f"[agent_query_ollama] bridge starting, {len(tm.tools)} tools, port {port}",
        flush=True,
    )

    # Conversation history (persists across queries in this bridge session)
    messages = [{"role": "system", "content": _build_system_prompt()}]
    query_lock = asyncio.Lock()

    async def send(writer, obj):
        writer.write((json.dumps(obj) + "\n").encode())
        await writer.drain()

    async def handle_client(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            if not line:
                return
            req = json.loads(line)
            qid = req.get("id", 0)

            if req.get("type") == "status_request":
                # Return tool list and basic info
                servers = []
                for name, client in tm.clients.items():
                    servers.append(
                        {
                            "name": name,
                            "status": "connected",
                            "error": "",
                            "scope": "",
                            "version": "",
                            "config_type": "",
                            "config_url": "",
                            "tools": [
                                {
                                    "name": t["function"]["name"],
                                    "description": t["function"]["description"],
                                    "readonly": False,
                                    "destructive": False,
                                }
                                for t in tm.tools
                                if t["_client"] == name
                            ],
                        }
                    )
                ctx = {
                    "model": _OLLAMA_MODEL,
                    "total_tokens": 0,
                    "max_tokens": 1000000,
                    "raw_max_tokens": 1000000,
                    "percent": 0,
                    "autocompact_enabled": False,
                    "autocompact_threshold": 0,
                    "autocompact_source": "",
                    "categories": [],
                    "memory_files": [],
                    "mcp_tools": [],
                    "system_tools": [],
                    "system_prompt_sections": [],
                    "api_usage": None,
                    "session_id": "",
                }
                await send(
                    writer,
                    {
                        "id": qid,
                        "type": "status_data",
                        "servers": servers,
                        "context": ctx,
                    },
                )
                return

            prompt = req.get("prompt", "")

            async def run_query():
                async with query_lock:
                    messages.append({"role": "user", "content": prompt})

                    # Tool-call loop: keep going until the model stops calling tools
                    max_turns = 15
                    for turn in range(max_turns):
                        # Call Ollama with tools
                        def _do_chat():
                            r = ollama.chat(
                                model=_OLLAMA_MODEL,
                                messages=messages,
                                tools=[
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": t["function"]["name"],
                                            "description": t["function"]["description"],
                                            "parameters": t["function"]["parameters"],
                                        },
                                    }
                                    for t in tm.tools
                                ],
                                stream=False,
                            )
                            # Normalize Pydantic response to dict
                            msg = r.message
                            return {
                                "content": msg.content or "",
                                "tool_calls": [
                                    {
                                        "id": f"call_{i}",
                                        "function": {
                                            "name": tc.function.name,
                                            "arguments": tc.function.arguments,
                                        },
                                    }
                                    for i, tc in enumerate(msg.tool_calls or [])
                                ],
                            }

                        resp = await asyncio.to_thread(_do_chat)
                        content = resp["content"]
                        tool_calls = resp["tool_calls"] if resp["tool_calls"] else None

                        # Send text to client
                        if content:
                            await send(
                                writer, {"id": qid, "type": "text", "text": content}
                            )

                        # Add assistant message to history
                        asst_msg = {"role": "assistant", "content": content or ""}
                        if tool_calls:
                            asst_msg["tool_calls"] = [
                                {
                                    "id": tc.get("id", f"call_{i}"),
                                    "type": "function",
                                    "function": {
                                        "name": tc["function"]["name"],
                                        "arguments": tc["function"]["arguments"],
                                    },
                                }
                                for i, tc in enumerate(tool_calls)
                            ]
                        messages.append(asst_msg)

                        if not tool_calls:
                            # No more tool calls — done
                            await send(
                                writer,
                                {
                                    "id": qid,
                                    "type": "done",
                                    "session_id": "",
                                    "duration_ms": 0,
                                    "cost": 0.0,
                                    "num_turns": turn + 1,
                                    "stop_reason": "end_turn",
                                    "model": _OLLAMA_MODEL,
                                    "context_window": {
                                        "used_percentage": 0,
                                        "total_input_tokens": 0,
                                        "context_window_size": 1000000,
                                    },
                                },
                            )
                            return

                        # Execute each tool call
                        for tc in tool_calls:
                            fn_name = tc["function"]["name"]
                            fn_args = tc["function"]["arguments"]
                            if isinstance(fn_args, str):
                                try:
                                    fn_args = json.loads(fn_args)
                                except Exception:
                                    pass

                            await send(
                                writer,
                                {
                                    "id": qid,
                                    "type": "tool_use",
                                    "tool_id": tc.get("id", "call_0"),
                                    "name": fn_name,
                                    "input": fn_args,
                                },
                            )

                            # Execute the tool
                            result = await asyncio.to_thread(tm.call, fn_name, fn_args)

                            await send(
                                writer,
                                {
                                    "id": qid,
                                    "type": "tool_result",
                                    "tool_id": tc.get("id", "call_0"),
                                    "is_error": False,
                                },
                            )

                            # Add tool result to history
                            messages.append(
                                {
                                    "role": "tool",
                                    "content": str(result)[:8000],
                                }
                            )

                    # Hit max turns
                    await send(
                        writer,
                        {
                            "id": qid,
                            "type": "done",
                            "session_id": "",
                            "duration_ms": 0,
                            "cost": 0.0,
                            "num_turns": max_turns,
                            "stop_reason": "max_turns",
                            "model": _OLLAMA_MODEL,
                            "context_window": {
                                "used_percentage": 0,
                                "total_input_tokens": 0,
                                "context_window_size": 1000000,
                            },
                        },
                    )

            query_task = asyncio.create_task(run_query())

            async def watch_for_interrupt():
                try:
                    buf = b""
                    while not query_task.done():
                        data = await reader.read(1024)
                        if not data:
                            break
                        buf += data
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            if not line.strip():
                                continue
                            try:
                                msg = json.loads(line)
                                if (
                                    msg.get("type") == "interrupt"
                                    and not query_task.done()
                                ):
                                    query_task.cancel()
                                    return
                            except Exception:
                                pass
                except Exception:
                    pass

            interrupt_task = asyncio.create_task(watch_for_interrupt())
            try:
                await query_task
            except asyncio.CancelledError:
                await send(writer, {"id": qid, "type": "stopped"})
            finally:
                interrupt_task.cancel()
                return
        except Exception as e:
            try:
                await send(writer, {"id": 0, "type": "error", "error": str(e)})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle_client, "127.0.0.1", port)
    async with server:
        await server.serve_forever()


async def main(prompt: str):
    """One-shot query."""
    import ollama

    server_configs = _load_mcp_servers()
    tm = ToolManager(server_configs)
    print(f"[agent_query_ollama] {len(tm.tools)} tools loaded", file=sys.stderr)

    messages = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": prompt},
    ]

    for turn in range(15000):

        def _do_chat():
            print(
                f"[aqo] calling ollama with {len(tm.tools)} tools, {len(messages)} messages",
                file=sys.stderr,
                flush=True,
            )
            r = ollama.chat(
                model=_OLLAMA_MODEL,
                messages=messages,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": t["function"]["name"],
                            "description": t["function"]["description"],
                            "parameters": t["function"]["parameters"],
                        },
                    }
                    for t in tm.tools
                ],
                stream=False,
            )
            msg = r.message
            print(
                f"[aqo] got response, content={msg.content[:50] if msg.content else None}, tool_calls={msg.tool_calls}",
                file=sys.stderr,
                flush=True,
            )
            return {
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for i, tc in enumerate(msg.tool_calls or [])
                ],
            }

        print(f"[aqo] turn {turn}, calling _do_chat", file=sys.stderr, flush=True)
        resp = await asyncio.to_thread(_do_chat)
        print(f"[aqo] turn {turn}, got resp", file=sys.stderr, flush=True)
        content = resp["content"]
        tool_calls = resp["tool_calls"] if resp["tool_calls"] else None

        if content:
            sys.stdout.write(content)
            sys.stdout.flush()

        asst_msg = {"role": "assistant", "content": content or ""}
        if tool_calls:
            asst_msg["tool_calls"] = [
                {
                    "id": tc.get("id", f"call_{i}"),
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
                for i, tc in enumerate(tool_calls)
            ]
        messages.append(asst_msg)

        if not tool_calls:
            break

        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            fn_args = tc["function"]["arguments"]
            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args)
                except Exception:
                    pass
            result = await asyncio.to_thread(tm.call, fn_name, fn_args)
            messages.append({"role": "tool", "content": str(result)[:8000]})

    tm.stop()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--bridge":
        asyncio.run(main_bridge(int(sys.argv[2])))
    else:
        prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Say hello."
        asyncio.run(main(prompt))
