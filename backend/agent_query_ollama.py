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

# MCP tool layer (lifted from jonigl/mcp-client-for-ollama, see mcpclient/)
from mcpclient.connector import ServerConnector
from mcpclient.config import load_mcp_servers


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

                            # Execute the tool (async via the mcp SDK)
                            result = await tm.call(fn_name, fn_args)

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