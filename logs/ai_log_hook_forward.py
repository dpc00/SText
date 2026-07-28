#!/usr/bin/env python3
"""Forward Grok/Claude-style hook stdin JSON to the local ai_log_server.

Grok blocks type=http hooks that use http:// (SSRF: https only). Claude and
other agents POST directly to http://127.0.0.1:9511/event. This command hook
reads the event envelope from stdin and POSTs it to the same endpoint so Grok
sessions land in ~/data/logs/<date>.md like everyone else.

Usage (from ~/.grok/hooks/*.json):
  { "type": "command", "command": "python .../ai_log_hook_forward.py", "timeout": 5 }

Exit 0 always (fail-open): a down log server must not block the agent.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:9511/event"
TIMEOUT = 4.0


def main() -> int:
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        return 0
    # Pass through as-is; ai_log_server normalizes camelCase / vendor variants.
    req = urllib.request.Request(
        URL,
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        # Fail open: missing log server is not an agent failure.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
