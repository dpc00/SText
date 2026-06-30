"""Async MCP server connector for the SText Ollama backend.

Slimmed from jonigl/mcp-client-for-ollama/server/connector.py (MIT). Drops the
rich Console, prompts/resources/templates discovery, and the interactive
server-selection path — this backend only needs tools. Logs to stderr as
plain text so it shows up in the ai_sdk bridge monitor.

Transport uses the official `mcp` SDK: stdio / sse / streamable_http. Tool
names are exposed to the model as `mcp__<server>__<tool>` — the same convention
Claude Code uses, so the model already knows these names.
"""

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


class ServerConnector:
    """Connects to one or more MCP servers and exposes their tools.

    Lifecycle: ``await connector.connect(configs)`` once at startup; then
    ``await connector.call_tool(full_name, arguments)`` per tool call;
    ``await connector.aclose()`` on shutdown.
    """

    def __init__(self):
        self.exit_stack = AsyncExitStack()
        # name -> {"session": ClientSession, "tools": [tool_dict, ...]}
        self.sessions = {}
        # flat list of tool dicts in the shape the Ollama loop + status handler expect:
        # {"type":"function", "function":{"name","description","parameters"},
        #  "_client": server_name, "_orig_name": orig_tool_name}
        self.tools = []

    async def connect(self, server_configs):
        """Connect to every server in {name: cfg}; skip failures, keep going."""
        for name, cfg in (server_configs or {}).items():
            # st-plugin is the ai_sdk.py socket bridge, not a real MCP server;
            # it isn't in ~/.claude.json but guard anyway in case it's added.
            if name == "st-plugin":
                continue
            try:
                await self._connect_one(name, cfg)
            except asyncio.CancelledError:
                _log(f"[mcp] {name}: timed out initializing, skipping")
            except Exception as e:
                # mcp SDK sometimes wraps multiple errors in an ExceptionGroup
                subs = getattr(e, "exceptions", None)
                if subs:
                    _log(f"[mcp] {name}: failed to connect: {'; '.join(str(s) for s in subs)}")
                else:
                    _log(f"[mcp] {name}: failed to connect: {e}")

    async def _connect_one(self, name, cfg):
        stype = cfg.get("type", "stdio")

        async with AsyncExitStack() as local:
            if stype == "sse":
                url = cfg.get("url")
                if not url:
                    _log(f"[mcp] {name}: sse server missing url, skipping")
                    return
                headers = {k.lower(): v for k, v in (cfg.get("headers") or {}).items()}
                transport = await local.enter_async_context(sse_client(url, headers=headers))
                read, write = transport
                session = await local.enter_async_context(ClientSession(read, write))

            elif stype in ("streamable_http", "http"):
                url = cfg.get("url")
                if not url:
                    _log(f"[mcp] {name}: http server missing url, skipping")
                    return
                transport = await local.enter_async_context(streamablehttp_client(url))
                read, write, _info = transport
                session = await local.enter_async_context(ClientSession(read, write))

            else:  # stdio (the default when no type given)
                command = cfg.get("command")
                if not command:
                    _log(f"[mcp] {name}: stdio server missing command, skipping")
                    return
                # StdioServerParameters.env=None inherits our process env. When a
                # server supplies env vars, merge them ON TOP of os.environ so
                # PATH (and npx/bun lookup) still works.
                env = cfg.get("env")
                if env:
                    env = {**os.environ, **env}
                params = StdioServerParameters(command=command, args=cfg.get("args", []), env=env)
                transport = await local.enter_async_context(stdio_client(params))
                read, write = transport
                session = await local.enter_async_context(ClientSession(read, write))

            await session.initialize()
            # Success — hand the connection contexts to the long-lived stack.
            await self.exit_stack.enter_async_context(local.pop_all())

        server_tools = []
        try:
            resp = await session.list_tools()
        except Exception as e:
            _log(f"[mcp] {name}: connected but list_tools failed: {e}")
            self.sessions[name] = {"session": session, "tools": []}
            return

        for tool in resp.tools:
            full_name = f"mcp__{name}__{tool.name}"
            server_tools.append({
                "type": "function",
                "function": {
                    "name": full_name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                },
                "_client": name,
                "_orig_name": tool.name,
            })
        self.sessions[name] = {"session": session, "tools": server_tools}
        self.tools.extend(server_tools)
        _log(f"[mcp] {name}: {len(server_tools)} tools loaded")

    async def call_tool(self, full_name, arguments):
        """Execute a tool by its qualified mcp__<server>__<tool> name.

        Returns the tool's text output as a string (matching the old backend's
        contract). Non-text content blocks are stringified.
        """
        for t in self.tools:
            if t["function"]["name"] == full_name:
                session = self.sessions[t["_client"]]["session"]
                result = await session.call_tool(t["_orig_name"], arguments)
                texts = []
                for block in getattr(result, "content", None) or []:
                    btype = getattr(block, "type", None)
                    if btype == "text" or hasattr(block, "text"):
                        texts.append(getattr(block, "text", "") or "")
                    else:
                        texts.append(json.dumps(getattr(block, "model_dump", lambda: {})()))
                return "\n".join(texts) if texts else ""
        return f"Error: tool {full_name} not found"

    async def aclose(self):
        """Shut down all server connections."""
        await self.exit_stack.aclose()