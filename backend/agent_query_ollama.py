"""agent_query_ollama.py — Stream an Ollama response to stdout, with MCP tool support.

Drop-in replacement for agent_query.py that uses Ollama instead of Claude Agent SDK.
Speaks the same JSON-lines TCP bridge protocol as the original.

Usage:
  python agent_query_ollama.py "your prompt here"     # one-shot
  python agent_query_ollama.py --bridge PORT           # persistent multi-turn bridge

Tool layer: real MCP transport (stdio/sse/streamable_http) via the official `mcp`
SDK, lifted from jonigl/mcp-client-for-ollama — see backend/mcpclient/. Servers are
loaded from ~/.claude.json mcpServers (the same source Claude Code uses).
"""

import asyncio
import json
import os
import re
import sys
import subprocess
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass  # ST's _LogWriter doesn't support reconfigure

_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "glm-5.2:cloud")
_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
_PYTHON = r"C:\Users\donal\AppData\Local\Programs\Python\Python312\python.exe"

# Settings file paths to probe for `spawn_env.OLLAMA_MODEL`. Live Packages
# location first (what Sublime actually loaded), then the SText repo copy.
# Reading the file directly is the only way for a subprocess to honor
# ai_terminal.sublime-settings — ST's settings API isn't available here.
_AI_TERMINAL_SETTINGS_PATHS = [
    os.path.expanduser(
        r"~\AppData\Roaming\Sublime Text\Packages\User\ai_terminal.sublime-settings"
    ),
    r"C:\Users\donal\projects\SText\ai_terminal.sublime-settings",
]


def _read_ollama_model_from_settings():
    """Parse ai_terminal.sublime-settings for spawn_env.OLLAMA_MODEL.

    Returns the value as a string, or None if not found / parse failed.
    Minimal JSON-like parser: tolerates a few leading comment lines and
    extracts the `spawn_env` block substring, then a quick `"OLLAMA_MODEL":`
    scan inside it. Avoids needing the json module to handle trailing commas
    / comments that a hand-edited settings file may have.
    """
    for path in _AI_TERMINAL_SETTINGS_PATHS:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue

        # Strip line comments and block comments
        cleaned = []
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("//"):
                continue
            cleaned.append(line)
        cleaned = "\n".join(cleaned)

        # Find the "spawn_env" object
        idx = cleaned.find('"spawn_env"')
        if idx < 0:
            continue
        brace = cleaned.find("{", idx)
        if brace < 0:
            continue
        depth = 0
        end = brace
        for j in range(brace, len(cleaned)):
            ch = cleaned[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        block = cleaned[brace : end + 1]

        # Inside the block, find "OLLAMA_MODEL": "value"
        m = re.search(
            r'"OLLAMA_MODEL"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', block
        )
        if m:
            # Unescape \\ -> \ and \" -> "
            return m.group(1).replace("\\\\", "\x00").replace('\\"', '"').replace("\x00", "\\")
    return None


# If the env var wasn't set in ST's process env, fall back to the settings
# file (ai_terminal.sublime-settings -> spawn_env.OLLAMA_MODEL). This makes
# the settings file the single source of truth.
if "OLLAMA_MODEL" not in os.environ:
    _settings_model = _read_ollama_model_from_settings()
    if _settings_model:
        _OLLAMA_MODEL = _settings_model

# Max tool-call iterations per query before the loop gives up. Mutable at
# runtime via the /loop-limit slash command (set_loop_limit bridge request).
_LOOP_LIMIT = 15

# Toggle thinking mode for models that support it (deepseek-r1, qwen3, gpt-oss,
# etc.). When True, ollama.chat is called with think=True and the model's
# reasoning is sent as a `thinking` event before the answer text.
_THINK = False

# Human-in-the-Loop: when True, each tool call is gated on an explicit
# approval from the ST side (tool_approval_request -> tool_approval). The
# query waits on an asyncio.Event that the socket watcher sets on receipt of
# the approval. Escapable: an interrupt cancels the query, which raises
# CancelledError out of the awaited event and falls through to "stopped".
_HIL = False


def _wrap_sync_iter(it):
    """Convert a sync iterator into an async one (yields on the caller's loop)."""
    async def _agen():
        for item in it:
            yield item
    return _agen()

# MCP tool layer (lifted from jonigl/mcp-client-for-ollama, see mcpclient/)
from mcpclient.connector import ServerConnector
from mcpclient.config import load_mcp_servers


# ─── Context files ───────────────────────────────────────────────────────────

# Context files (governed by ai_terminal.sublime-settings) — see the
# `context_files` and `system_prompt_wrapper` keys in that file. Moved
# out of the bridge so the user can edit context without editing code.


def _read_setting_from_json(key):
    """Read a string-or-list value from the live Packages copy of
    ai_terminal.sublime-settings. Returns None if the key is missing or the
    file can't be parsed. Falls back to the SText repo copy.

    Tolerates JS-style line comments and raw newlines inside quoted string
    values (raw newlines become \n in the returned value), so the user can
    write a multi-line wrapper without using \\n escapes:

        "system_prompt_wrapper": "First line.
        Second line.
        Third line."

    ST's own settings parser is strict JSON and doesn't allow raw newlines
    in strings, so this would fail if ST tried to load the file. But the
    bridge reads the file directly (with its own parser), so we have more
    freedom than the live settings system does.
    """
    for path in _AI_TERMINAL_SETTINGS_PATHS:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        # Strip JS-style line comments (// ...) — the settings file uses them.
        # Strip the comment portion but keep the trailing newline so the
        # line count is preserved for any later parse.
        cleaned_lines = []
        for line in text.splitlines(keepends=True):
            # Comment-only lines: drop the whole line including the newline
            stripped = line.lstrip()
            if stripped.startswith("//"):
                continue
            # Inline comment: keep the leading content, drop the comment.
            # Naive but adequate — a // inside a quoted string is rare and
            # we'd rather over-strip than under-strip.
            idx = line.find("//")
            if idx >= 0:
                line = line[:idx].rstrip() + "\n"
            cleaned_lines.append(line)
        cleaned = "".join(cleaned_lines)
        # Strip block comments
        cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
        # Extract the value for `key` — handles strings (which may contain
        # raw newlines that we turn into \n) and arrays of strings. The
        # string-content class [^"\\] is widened to [^"\\\n] so the regex
        # can match a multi-line quoted string. We rely on the fact that
        # the comment-stripping step above has already removed any // that
        # could appear inside the string.
        m = re.search(
            rf'"{re.escape(key)}"\s*:\s*("([^"\\\\\n]*(?:\\.[^"\\\\\n]*)*)"|\[([^\]]*)\])',
            cleaned,
            flags=re.DOTALL,
        )
        if not m:
            return None
        if m.group(1):  # string value
            raw = m.group(2)
            return (
                raw.replace("\\\\", "\x00")
                .replace('\\"', '"')
                .replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace("\x00", "\\")
            )
        # array value — extract each quoted string
        items = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(3) or "")
        return [
            s.replace("\\\\", "\x00").replace('\\"', '"').replace("\x00", "\\")
            for s in items
        ]
    return None


def _build_system_prompt():
    files = _read_setting_from_json("context_files") or [
        r"C:\Users\donal\agents.md",
        r"C:\Users\donal\router.md",
        r"C:\Users\donal\projects\SText\CLAUDE.md",
    ]
    parts = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                parts.append(f"# {os.path.basename(path)}\n{f.read()}")
        except OSError:
            pass
    context = "\n\n---\n\n".join(parts)
    wrapper = _read_setting_from_json("system_prompt_wrapper") or (
        "You are an AI coding assistant running inside Sublime Text via the "
        "ai_sdk plugin.\nYou have access to MCP tools for Sublime Text "
        "(sublime-mcp), screenshots, web scraping (firecrawl), and more.\n"
        "Use tools when they help. Be concise and direct."
    )
    return f"{wrapper}\n\n{context}"


# ─── Built-in tools (implemented locally, not via MCP) ─────────────────────────
# Mirrors the harness tools Claude Code injects (Bash/Read/etc.). These live in the
# backend, NOT in ~/.claude.json, so Claude Code never sees them — no pollution.
# Tool dict shape matches the MCP tools so the Ollama loop consumes them unchanged.


def _bi_run_shell(command, timeout=30, **_):
    """Run a shell command (cmd.exe on Windows) and return stdout/stderr/exit_code."""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=os.path.expanduser("~"),
        )
        return {
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-2000:],
            "exit_code": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"timed out after {timeout}s", "exit_code": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1}


_BUILTIN_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Run a shell command on the local machine (cmd.exe on Windows) and "
                "return stdout, stderr, and exit_code. Use for listing files, git, "
                "python, etc. cwd is the user's home directory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to wait (default 30).",
                        "default": 30,
                    },
                },
                "required": ["command"],
            },
        },
        "_client": "builtins",
        "_orig_name": "run_shell",
        "_impl": _bi_run_shell,
    }
]
_BUILTIN_BY_NAME = {t["function"]["name"]: t for t in _BUILTIN_DEFS}


# ─── Tool manager ────────────────────────────────────────────────────────────


class ToolManager:
    """Async wrapper over ServerConnector.

    Exposes ``self.tools`` in the same dict shape the Ollama loop and the
    status handler already consume, so those call sites are unchanged:
        {"type": "function",
         "function": {"name", "description", "parameters"},
         "_client": server_name, "_orig_name": orig_tool_name}
    """

    def __init__(self, server_configs):
        self._configs = server_configs or {}
        self.connector = ServerConnector()
        self.tools = []
        # Retired connectors are kept referenced (never closed/GC'd) because
        # closing MCP transports fires an anyio cancel scope that cascades into
        # the bridge's serve_forever(). Leaks the old MCP subprocesses per reload.
        self._retired = []

    async def start(self):
        await self.connector.connect(self._configs)
        self.tools = list(_BUILTIN_DEFS) + self.connector.tools
        print(
            f"[agent_query_ollama] {len(self.connector.tools)} MCP tools from "
            f"{len(self.connector.sessions)} server(s) + {len(_BUILTIN_DEFS)} built-in "
            f"= {len(self.tools)} total",
            file=sys.stderr,
            flush=True,
        )

    async def call(self, full_name, arguments):
        bi = _BUILTIN_BY_NAME.get(full_name)
        if bi is not None:
            return await asyncio.to_thread(bi["_impl"], **arguments)
        return await self.connector.call_tool(full_name, arguments)

    async def reload(self):
        """Reconnect to MCP servers (in-process). The old connector is retired
        (kept referenced, not closed) to avoid the anyio cancel-scope cascade
        that kills serve_forever() when MCP transports are closed."""
        self._retired.append(self.connector)
        self.connector = ServerConnector()
        await self.connector.connect(self._configs)
        self.tools = list(_BUILTIN_DEFS) + self.connector.tools
        return len(self.connector.tools), len(self.tools)

    async def stop(self):
        await self.connector.aclose()


# ─── Ollama bridge ───────────────────────────────────────────────────────────


async def main_bridge(port: int):
    """Persistent bridge: multi-turn Ollama chat, listens on TCP port."""
    import ollama

    server_configs = load_mcp_servers()
    tm = ToolManager(server_configs)
    await tm.start()
    print(
        f"[agent_query_ollama] bridge starting, {len(tm.tools)} tools, port {port}",
        flush=True,
    )

    # Conversation history (persists across queries in this bridge session).
    # Auto-loaded from disk on startup and auto-saved after every change so
    # the conversation survives bridge restarts, plugin reloads, and ST
    # itself restarting. File path: $cwd/.cache/ai_sdk_history.json (falls
    # back to the user's home dir if cwd isn't writable).
    def _history_path():
        for d in (os.getcwd(), os.path.expanduser("~")):
            try:
                p = os.path.join(d, ".cache", "ai_sdk_history.json")
                os.makedirs(os.path.dirname(p), exist_ok=True)
                return p
            except OSError:
                continue
        return None

    def _save_history():
        p = _history_path()
        if not p:
            return
        try:
            # Filter to ollama-compatible fields and drop tool_calls/tool
            # results (those reference runtime tool_call_ids that don't
            # round-trip cleanly across bridge restarts).
            slim = [
                {"role": m.get("role"), "content": m.get("content", "")}
                for m in messages
                if m.get("role") in ("system", "user", "assistant")
                and isinstance(m.get("content"), str)
            ]
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(slim, f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
        except Exception as e:
            print(f"[agent_query_ollama] history save failed: {e}", file=sys.stderr)

    def _load_history():
        p = _history_path()
        if not p or not os.path.exists(p):
            return []
        try:
            with open(p, encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, list):
                return []
            # Drop the auto-injected system prompt from the saved history so
            # we don't double-stack it; we re-prepend the fresh one.
            return [m for m in loaded if m.get("role") != "system"]
        except Exception as e:
            print(f"[agent_query_ollama] history load failed: {e}", file=sys.stderr)
            return []

    _loaded = _load_history()

    class _History:
        """list-like wrapper that auto-saves to disk after every append.

        Persists the conversation across bridge restarts, plugin reloads,
        and ST itself restarting. Reads return a fresh list snapshot so
        callers can iterate safely; writes go through append().
        """

        def __init__(self, initial):
            self._list = list(initial)

        def append(self, item):
            self._list.append(item)
            _save_history()

        def extend(self, items):
            self._list.extend(items)
            _save_history()

        def __iter__(self):
            return iter(self._list)

        def __len__(self):
            return len(self._list)

        def __getitem__(self, idx):
            return self._list[idx]

        def __setitem__(self, idx, val):
            self._list[idx] = val
            _save_history()

        def clear(self):
            self._list.clear()
            _save_history()

        def snapshot(self):
            return list(self._list)

    messages = _History([{"role": "system", "content": _build_system_prompt()}] + _loaded)
    if _loaded:
        print(
            f"[agent_query_ollama] loaded {len(_loaded)} prior messages from history",
            flush=True,
        )
    # Tool names the user has muted via /tools off — filtered out before the
    # Ollama call. Persisted across queries like messages.
    disabled = set()
    query_lock = asyncio.Lock()

    async def send(writer, obj):
        writer.write((json.dumps(obj) + "\n").encode())
        await writer.drain()

    async def handle_client(reader, writer):
        global _OLLAMA_MODEL, _LOOP_LIMIT, _THINK, _HIL
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            if not line:
                return
            req = json.loads(line)
            qid = req.get("id", 0)

            if req.get("type") == "status_request":
                # Return tool list and basic info
                servers = []
                for name, sinfo in tm.connector.sessions.items():
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
                # Built-in (non-MCP) tools — surface as a pseudo-server so they're
                # visible in the status panel alongside the MCP servers.
                bi_tools = [t for t in tm.tools if t.get("_client") == "builtins"]
                if bi_tools:
                    servers.append(
                        {
                            "name": "builtins",
                            "status": "connected",
                            "error": "",
                            "scope": "system",
                            "version": "1",
                            "config_type": "builtin",
                            "config_url": "",
                            "tools": [
                                {
                                    "name": t["function"]["name"],
                                    "description": t["function"]["description"],
                                    "readonly": False,
                                    "destructive": False,
                                }
                                for t in bi_tools
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

            if req.get("type") == "export_history":
                path = os.path.expanduser(req.get("path", ""))
                try:
                    parent = os.path.dirname(path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(messages, f, ensure_ascii=False, indent=2)
                    await send(
                        writer, {"id": qid, "type": "history_exported", "path": path}
                    )
                except Exception as e:
                    await send(writer, {"id": qid, "type": "error", "error": str(e)})
                return

            if req.get("type") == "import_history":
                path = os.path.expanduser(req.get("path", ""))
                try:
                    with open(path, encoding="utf-8") as f:
                        loaded = json.load(f)
                    if not isinstance(loaded, list):
                        raise ValueError("history file is not a list of messages")
                    turns = sum(1 for m in loaded if m.get("role") in ("user", "assistant"))
                    messages.clear()
                    messages.extend(loaded)
                    await send(
                        writer,
                        {
                            "id": qid,
                            "type": "history_imported",
                            "path": path,
                            "turns": turns,
                        },
                    )
                except Exception as e:
                    await send(writer, {"id": qid, "type": "error", "error": str(e)})
                return

            if req.get("type") == "clear_history":
                # In-place reset: drop conversation but keep the system prompt
                # and the running bridge (no subprocess restart, no MCP
                # reconnect). Cheaper and faster than /restart.
                messages.clear()
                messages.append(
                    {"role": "system", "content": _build_system_prompt()}
                )
                await send(writer, {"id": qid, "type": "history_cleared"})
                return

            if req.get("type") == "set_loop_limit":
                n = req.get("limit", 0)
                if not isinstance(n, int) or n < 1:
                    await send(
                        writer,
                        {
                            "id": qid,
                            "type": "error",
                            "error": "limit must be a positive integer",
                        },
                    )
                    return
                _LOOP_LIMIT = n
                await send(writer, {"id": qid, "type": "loop_limit_set", "limit": n})
                return

            if req.get("type") == "list_models":
                try:
                    def _list():
                        return [x.model for x in ollama.list().models]

                    names = await asyncio.to_thread(_list)
                except Exception as e:
                    await send(writer, {"id": qid, "type": "error", "error": str(e)})
                    return
                await send(
                    writer,
                    {
                        "id": qid,
                        "type": "model_list",
                        "current": _OLLAMA_MODEL,
                        "models": names,
                    },
                )
                return

            if req.get("type") == "set_model":
                m = req.get("model", "")
                if not m:
                    await send(
                        writer, {"id": qid, "type": "error", "error": "no model name"}
                    )
                    return
                _OLLAMA_MODEL = m
                await send(writer, {"id": qid, "type": "model_set", "model": m})
                return

            if req.get("type") == "reload_model_from_settings":
                # Re-read ai_terminal.sublime-settings and pick up
                # spawn_env.OLLAMA_MODEL if present. Use this when the
                # settings file changed but the bridge was already running.
                new_model = _read_ollama_model_from_settings()
                if new_model and new_model != _OLLAMA_MODEL:
                    _OLLAMA_MODEL = new_model
                await send(
                    writer,
                    {
                        "id": qid,
                        "type": "model_reloaded",
                        "model": _OLLAMA_MODEL,
                        "source": "settings" if new_model else "unchanged",
                    },
                )
                return

            if req.get("type") == "set_thinking":
                enabled = bool(req.get("enabled", False))
                _THINK = enabled
                await send(
                    writer, {"id": qid, "type": "thinking_set", "enabled": enabled}
                )
                return

            if req.get("type") == "set_hil":
                enabled = bool(req.get("enabled", False))
                _HIL = enabled
                await send(writer, {"id": qid, "type": "hil_set", "enabled": enabled})
                return

            if req.get("type") == "list_prompts":
                await send(
                    writer,
                    {"id": qid, "type": "prompts_list",
                     "prompts": tm.connector.all_prompts()},
                )
                return

            if req.get("type") == "get_prompt":
                server = req.get("server", "")
                pname = req.get("name", "")
                arguments = req.get("arguments", {}) or {}
                try:
                    msgs = await tm.connector.get_prompt(server, pname, arguments)
                    if msgs is None:
                        await send(writer, {
                            "id": qid, "type": "error",
                            "error": f"no such server: {server}",
                        })
                        return
                    await send(writer, {
                        "id": qid, "type": "prompt_result", "messages": msgs,
                    })
                except Exception as e:
                    await send(writer, {"id": qid, "type": "error", "error": str(e)})
                return

            if req.get("type") == "list_resources":
                await send(
                    writer,
                    {"id": qid, "type": "resources_list",
                     "resources": tm.connector.all_resources()},
                )
                return

            if req.get("type") == "read_resource":
                uri = req.get("uri", "")
                try:
                    text = await tm.connector.read_resource(uri)
                    if text is None:
                        await send(writer, {
                            "id": qid, "type": "error",
                            "error": f"resource not found: {uri}",
                        })
                        return
                    await send(writer, {
                        "id": qid, "type": "resource_read", "text": text,
                    })
                except Exception as e:
                    await send(writer, {"id": qid, "type": "error", "error": str(e)})
                return

            if req.get("type") == "reload_servers":
                try:
                    mcp_count, total = await tm.reload()
                except Exception as e:
                    await send(writer, {"id": qid, "type": "error", "error": str(e)})
                    return
                await send(
                    writer,
                    {
                        "id": qid,
                        "type": "servers_reloaded",
                        "mcp_tools": mcp_count,
                        "total_tools": total,
                    },
                )
                return

            if req.get("type") == "list_tools":
                tools_info = [
                    {
                        "name": t["function"]["name"],
                        "description": t["function"].get("description", ""),
                        "client": t.get("_client", ""),
                        "enabled": t["function"]["name"] not in disabled,
                    }
                    for t in tm.tools
                ]
                await send(
                    writer, {"id": qid, "type": "tools_list", "tools": tools_info}
                )
                return

            if req.get("type") == "set_tool":
                name = req.get("name", "")
                enabled = req.get("enabled", True)
                if not name:
                    await send(
                        writer, {"id": qid, "type": "error", "error": "no tool name"}
                    )
                    return
                if enabled:
                    disabled.discard(name)
                else:
                    disabled.add(name)
                await send(
                    writer,
                    {"id": qid, "type": "tool_set", "name": name, "enabled": enabled},
                )
                return

            prompt = req.get("prompt", "")

            # HIL: tool_id -> {"event": asyncio.Event, "approve": bool|None}.
            # Populated by run_query when it gates on a tool call; drained by
            # the socket watcher when the ST side sends tool_approval.
            pending_approvals = {}

            async def run_query():
                async with query_lock:
                    messages.append({"role": "user", "content": prompt})
                    t0 = time.time()

                    # Tool-call loop: keep going until the model stops calling tools
                    max_turns = _LOOP_LIMIT
                    total_in = 0
                    total_out = 0
                    for turn in range(max_turns):
                        active_tools = [
                            t for t in tm.tools if t["function"]["name"] not in disabled
                        ]

                        # Stream the model response chunk by chunk. Text and
                        # thinking tokens are forwarded to the client as
                        # they're produced (text_delta / thinking_delta
                        # events). Tool calls are accumulated by index —
                        # Ollama emits a fresh, more-complete tool_calls
                        # array on later chunks, so we keep the latest
                        # version of each index.
                        accumulated_content = ""
                        accumulated_thinking = ""
                        # list of tool-call dicts keyed by index; last write wins
                        tool_calls_by_index = {}

                        def _chat_call():
                            return ollama.chat(
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
                                    for t in active_tools
                                ],
                                stream=True,
                                think=_THINK,
                            )

                        stream = await asyncio.to_thread(_chat_call)
                        try:
                            # ollama Python lib: stream=True returns a sync
                            # ChatResponse iterator, not async. Wrap it.
                            if hasattr(stream, "__aiter__"):
                                aiter = stream
                            else:
                                aiter = _wrap_sync_iter(stream)
                            async for chunk in aiter:
                                # Metrics (Ollama attaches them to the final chunk)
                                chunk_in = getattr(chunk, "prompt_eval_count", 0) or 0
                                chunk_out = getattr(chunk, "eval_count", 0) or 0
                                if chunk_in or chunk_out:
                                    # final chunk will accumulate below
                                    pass

                                msg = getattr(chunk, "message", None)
                                if msg is None:
                                    continue

                                # Stream thinking tokens incrementally
                                thinking_chunk = getattr(msg, "thinking", None) or ""
                                if thinking_chunk:
                                    accumulated_thinking += thinking_chunk
                                    await send(
                                        writer,
                                        {
                                            "id": qid,
                                            "type": "thinking_delta",
                                            "text": thinking_chunk,
                                        },
                                    )

                                # Stream text tokens incrementally
                                content_chunk = msg.content or ""
                                if content_chunk:
                                    accumulated_content += content_chunk
                                    await send(
                                        writer,
                                        {
                                            "id": qid,
                                            "type": "text_delta",
                                            "text": content_chunk,
                                        },
                                    )

                                # Accumulate tool calls (replace by index —
                                # later chunks have the more-complete version)
                                if msg.tool_calls:
                                    for i, tc in enumerate(msg.tool_calls):
                                        tool_calls_by_index[i] = {
                                            "id": f"call_{i}",
                                            "function": {
                                                "name": tc.function.name,
                                                "arguments": tc.function.arguments,
                                            },
                                        }

                                # Final chunk carries the metric counts
                                if chunk_in or chunk_out:
                                    total_in += chunk_in
                                    total_out += chunk_out
                        except Exception as exc:
                            print(
                                f"[aqo] stream error: {type(exc).__name__}: {exc}",
                                file=sys.stderr, flush=True,
                            )
                            pass

                        content = accumulated_content
                        thinking = accumulated_thinking
                        tool_calls = (
                            [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)]
                            if tool_calls_by_index
                            else None
                        )

                        # Send reasoning first (if the model produced any) — this
                        # is the legacy 'thinking' event for the @done footer
                        # path; the live stream already sent thinking_delta.
                        # (No-op for non-thinking models.)

                        # (Text was already streamed as text_delta events;
                        # no need to re-send the full content here.)

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
                                    "duration_ms": int((time.time() - t0) * 1000),
                                    "cost": 0.0,
                                    "num_turns": turn + 1,
                                    "stop_reason": "end_turn",
                                    "model": _OLLAMA_MODEL,
                                    "context_window": {
                                        "used_percentage": min(
                                            100, int(100 * total_in / 1000000)
                                        ),
                                        "total_input_tokens": total_in,
                                        "total_output_tokens": total_out,
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

                            tid = tc.get("id", "call_0")
                            await send(
                                writer,
                                {
                                    "id": qid,
                                    "type": "tool_use",
                                    "tool_id": tid,
                                    "name": fn_name,
                                    "input": fn_args,
                                },
                            )

                            # HIL gate: wait for the ST side to approve before
                            # executing. Escapable via interrupt (cancels the
                            # query -> CancelledError out of ev.wait()).
                            if _HIL:
                                ev = asyncio.Event()
                                pending_approvals[tid] = {"event": ev, "approve": None}
                                await send(
                                    writer,
                                    {
                                        "id": qid,
                                        "type": "tool_approval_request",
                                        "tool_id": tid,
                                        "name": fn_name,
                                        "input": fn_args,
                                    },
                                )
                                await ev.wait()
                                entry = pending_approvals.pop(tid, {})
                                if not entry.get("approve"):
                                    await send(
                                        writer,
                                        {
                                            "id": qid,
                                            "type": "tool_result",
                                            "tool_id": tid,
                                            "is_error": True,
                                            "rejected": True,
                                        },
                                    )
                                    messages.append(
                                        {
                                            "role": "tool",
                                            "content": "Tool call rejected by the user.",
                                        }
                                    )
                                    continue

                            # Execute the tool (async via the mcp SDK)
                            result = await tm.call(fn_name, fn_args)

                            await send(
                                writer,
                                {
                                    "id": qid,
                                    "type": "tool_result",
                                    "tool_id": tid,
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
                                "used_percentage": min(
                                    100, int(100 * total_in / 1000000)
                                ),
                                "total_input_tokens": total_in,
                                "total_output_tokens": total_out,
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
                                mtype = msg.get("type")
                                if mtype == "interrupt" and not query_task.done():
                                    query_task.cancel()
                                    return
                                if mtype == "tool_approval":
                                    entry = pending_approvals.get(msg.get("tool_id"))
                                    if entry is not None:
                                        entry["approve"] = bool(msg.get("approve"))
                                        entry["event"].set()
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

    server_configs = load_mcp_servers()
    tm = ToolManager(server_configs)
    await tm.start()

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
            result = await tm.call(fn_name, fn_args)
            messages.append({"role": "tool", "content": str(result)[:8000]})

    await tm.stop()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--bridge":
        asyncio.run(main_bridge(int(sys.argv[2])))
    else:
        prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Say hello."
        asyncio.run(main(prompt))