"""Async MCP server connector for the SText Ollama backend.

Slimmed from jonigl/mcp-client-for-ollama/server/connector.py (MIT). Drops the
rich Console and the interactive server-selection path. Surfaces the three
core MCP primitives the backend needs: tools, prompts, and resources.
Logs to stderr as plain text so it shows up in the ai_sdk bridge monitor.

Transport uses the official `mcp` SDK: stdio / sse / streamable_http. Tool
names are exposed to the model as `mcp__<server>__<tool>` — the same convention
Claude Code uses, so the model already knows these names.
"""

import asyncio
import json
import os
import shutil
import sys
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _resolve_stdio_command(command, args):
    """Resolve bare Windows shims (npx/bun) to absolute executables.

    Hosts that spawn without a shell (and some CreateProcess paths) cannot
    find bare ``npx`` — only ``npx.cmd`` / full paths. Prefer absolute paths
    so PATH-less GUI launches still work.
    """
    if not command:
        return command, list(args or [])
    args = list(args or [])
    resolved = command
    # Already absolute and exists — keep.
    if os.path.isabs(command) and os.path.exists(command):
        resolved = command
    else:
        which = shutil.which(command)
        if which:
            resolved = which
        elif sys.platform == "win32":
            base = os.path.basename(command).lower()
            # Common install locations when PATH is stripped (Explorer/ST/JVM).
            candidates = []
            if base in ("npx", "npx.cmd"):
                candidates = [
                    r"C:\Program Files\nodejs\npx.cmd",
                    os.path.expandvars(r"%ProgramFiles%\nodejs\npx.cmd"),
                ]
            elif base in ("bun", "bun.exe"):
                candidates = [
                    os.path.expanduser(r"~\.bun\bin\bun.exe"),
                    os.path.expandvars(r"%USERPROFILE%\.bun\bin\bun.exe"),
                ]
            elif base in ("node", "node.exe"):
                candidates = [
                    r"C:\Program Files\nodejs\node.exe",
                    os.path.expandvars(r"%ProgramFiles%\nodejs\node.exe"),
                ]
            for c in candidates:
                if c and os.path.exists(c):
                    resolved = c
                    break
    if resolved != command:
        _log(f"[mcp] resolved command {command!r} -> {resolved!r}")
    return resolved, args


def _stdio_env(cfg_env):
    """Build env for stdio servers: always keep a usable PATH for npx/node/bun."""
    env = {**os.environ}
    if cfg_env:
        env.update({str(k): str(v) for k, v in cfg_env.items()})
    # Ensure nodejs + bun bins are on PATH even if parent env is minimal.
    extras = []
    for p in (
        r"C:\Program Files\nodejs",
        os.path.expandvars(r"%ProgramFiles%\nodejs"),
        os.path.expanduser(r"~\.bun\bin"),
        os.path.expandvars(r"%APPDATA%\npm"),
    ):
        if p and os.path.isdir(p) and p not in extras:
            extras.append(p)
    path = env.get("PATH") or env.get("Path") or ""
    parts = [p for p in path.split(os.pathsep) if p]
    for p in reversed(extras):
        if p.lower() not in {x.lower() for x in parts}:
            parts.insert(0, p)
    env["PATH"] = os.pathsep.join(parts)
    return env


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
                command, args = _resolve_stdio_command(command, cfg.get("args", []))
                # Always pass a full env with PATH that includes nodejs/bun.
                # Bare env=None uses the SDK's reduced default env, which can
                # miss user-scoped bins depending on how the parent was launched.
                env = _stdio_env(cfg.get("env"))
                params = StdioServerParameters(command=command, args=args, env=env)
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

        # Prompts and resources are optional MCP primitives — many servers
        # don't expose them, so failures here are non-fatal. We deliberately
        # stay quiet for the common "Method not found" case (server doesn't
        # implement the primitive) so the python console doesn't fill up
        # with expected warnings every bridge start. Real errors still log.
        prompts = []
        try:
            presp = await session.list_prompts()
            for p in presp.prompts:
                prompts.append({
                    "name": p.name,
                    "description": getattr(p, "description", "") or "",
                    "arguments": [
                        {
                            "name": a.name,
                            "description": getattr(a, "description", "") or "",
                            "required": bool(getattr(a, "required", False)),
                        }
                        for a in (p.arguments or [])
                    ],
                })
        except Exception as e:
            if "method not found" not in str(e).lower():
                _log(f"[mcp] {name}: list_prompts failed: {e}")
        self.sessions[name]["prompts"] = prompts

        resources = []
        try:
            rresp = await session.list_resources()
            for r in rresp.resources:
                resources.append({
                    "uri": str(r.uri),
                    "name": getattr(r, "name", "") or "",
                    "description": getattr(r, "description", "") or "",
                    "mimeType": getattr(r, "mimeType", "") or "",
                })
        except Exception as e:
            if "method not found" not in str(e).lower():
                _log(f"[mcp] {name}: list_resources failed: {e}")
        self.sessions[name]["resources"] = resources
        if prompts or resources:
            _log(
                f"[mcp] {name}: {len(prompts)} prompts, "
                f"{len(resources)} resources"
            )

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

    # ─── Prompts ────────────────────────────────────────────────────────────

    def all_prompts(self):
        """Flat list of every prompt across all servers, tagged with `server`."""
        out = []
        for name, s in self.sessions.items():
            for p in s.get("prompts", []):
                out.append({**p, "server": name})
        return out

    async def get_prompt(self, server, name, arguments):
        """Invoke a prompt. Returns a list of {role, text} dicts."""
        if server not in self.sessions:
            return None
        session = self.sessions[server]["session"]
        result = await session.get_prompt(name, arguments or {})
        out = []
        for m in result.messages:
            content = m.content
            if hasattr(content, "text"):
                text = content.text or ""
            else:
                text = json.dumps(getattr(content, "model_dump", lambda: {})())
            out.append({"role": m.role, "text": text})
        return out

    # ─── Resources ──────────────────────────────────────────────────────────

    def all_resources(self):
        """Flat list of every resource across all servers, tagged with `server`."""
        out = []
        for name, s in self.sessions.items():
            for r in s.get("resources", []):
                out.append({**r, "server": name})
        return out

    async def read_resource(self, uri):
        """Read a resource by URI. Finds the owning session by URI match;
        falls back to trying each session directly. Returns text or None."""
        for name, s in self.sessions.items():
            for r in s.get("resources", []):
                if r["uri"] == uri:
                    result = await s["session"].read_resource(uri)
                    return "\n".join(
                        getattr(c, "text", "") or "" for c in result.contents
                    )
        # Not in any list — try each session (server may expose unlisted URIs)
        for name, s in self.sessions.items():
            try:
                result = await s["session"].read_resource(uri)
                return "\n".join(
                    getattr(c, "text", "") or "" for c in result.contents
                )
            except Exception:
                continue
        return None

    async def aclose(self):
        """Shut down all server connections."""
        await self.exit_stack.aclose()