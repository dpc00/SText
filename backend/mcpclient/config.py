"""MCP server config loader for the Ollama backend.

Reads mcpServers from ~/.claude.json (global + per-project) — the same source
Claude Code uses. The previous Ollama backend read ~/.ollama/config.json,
which has no mcpServers key, so zero tools were ever loaded. That was the
root cause of "it can't find any tools".
"""

import json
import os


def load_mcp_servers():
    """Return {name: cfg} merged from ~/.claude.json global + per-project mcpServers.

    Each cfg is the raw Claude Code entry: stdio servers have command/args/env;
    sse servers have type='sse' and url; streamable_http servers have
    type='streamable_http' and url. Servers with no 'type' default to stdio.
    """
    servers = {}
    try:
        cfg = json.load(open(os.path.expanduser("~/.claude.json"), encoding="utf-8"))
        servers.update(cfg.get("mcpServers", {}))
        for proj in cfg.get("projects", {}).values():
            servers.update(proj.get("mcpServers", {}))
    except Exception:
        pass
    return servers