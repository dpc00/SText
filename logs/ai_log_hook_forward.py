#!/usr/bin/env python3
"""Forward Grok/Claude-style hook stdin JSON to the local ai_log_server.

Grok blocks type=http hooks that use http:// (SSRF: https only). Claude and
other agents POST directly to http://127.0.0.1:9511/event. This command hook
reads the event envelope from stdin and POSTs it to the same endpoint so Grok
sessions land in ~/data/logs/<date>.md like everyone else.

When the log server is down, events are spooled under ~/data/logs/.hook_spool/
and drained on the next successful POST so UserPromptSubmit is not lost (empty
"You" sections were caused by PreToolUse recreating a session after a dropped
prompt event).

Usage (from ~/.grok/hooks/*.json):
  { "type": "command", "command": "python .../ai_log_hook_forward.py", "timeout": 5 }

Exit 0 always (fail-open): a down log server must not block the agent.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

URL = "http://127.0.0.1:9511/event"
TIMEOUT = 2.5  # stay under Grok's default 5s hook budget
SPOOL = os.path.join(os.path.expanduser("~"), "data", "logs", ".hook_spool")
MAX_DRAIN = 40


def _post(data: bytes) -> bool:
    req = urllib.request.Request(
        URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _spool(data: bytes) -> None:
    try:
        os.makedirs(SPOOL, exist_ok=True)
        name = f"{time.time():.6f}_{uuid.uuid4().hex[:8]}.json"
        path = os.path.join(SPOOL, name)
        with open(path, "wb") as f:
            f.write(data)
    except OSError:
        pass


def _drain() -> None:
    try:
        names = sorted(os.listdir(SPOOL))
    except OSError:
        return
    for name in names[:MAX_DRAIN]:
        if not name.endswith(".json"):
            continue
        path = os.path.join(SPOOL, name)
        try:
            with open(path, "rb") as f:
                data = f.read()
            if _post(data):
                try:
                    os.remove(path)
                except OSError:
                    pass
            else:
                break  # server still down; keep remaining spool
        except OSError:
            continue


def _tag_agent(raw: bytes) -> bytes:
    """Stamp agent=Grok so the daily log labels Grok turns (not Claude).

    Claude Code posts HTTP hooks directly and never hits this forwarder.
    ai_log_server defaults unlabeled turns to Claude for that path.
    """
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return raw
    if not isinstance(obj, dict):
        return raw
    # Do not overwrite an explicit agent from the client.
    if not obj.get("agent") and not obj.get("agentName"):
        obj["agent"] = "Grok"
    try:
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return raw


def main() -> int:
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        return 0
    raw = _tag_agent(raw)
    # Pass through; ai_log_server normalizes camelCase / vendor variants.
    if _post(raw):
        _drain()
    else:
        _spool(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
