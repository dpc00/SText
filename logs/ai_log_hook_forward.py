#!/usr/bin/env python3
"""Forward Grok/Claude-style hook stdin JSON to the local ai_log_server.

Grok blocks type=http hooks that use http:// (SSRF: https only). Claude and
other agents POST directly to http://10.0.0.56:9511/event. This command hook
reads the event envelope from stdin and POSTs it to the same endpoint so Grok
sessions land in ~/data/logs/<date>.md like everyone else.

When the log server is down, events are spooled under ~/data/logs/.hook_spool/
and drained on the next successful POST so UserPromptSubmit is not lost (empty
"You" sections were caused by PreToolUse recreating a session after a dropped
prompt event).

Usage (from an agent hook configuration):
  { "type": "command", "command": "python .../ai_log_hook_forward.py Grok", "timeout": 5 }
  { "type": "command", "command": "python .../ai_log_hook_forward.py Codex", "timeout": 5 }

The optional argument supplies the agent label and defaults to Grok for the
existing Grok configuration. Codex Stop/SubagentStop hooks receive an empty JSON
object on stdout because Codex requires valid JSON from those event handlers.

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

URL = "http://10.0.0.56:9511/event"
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


def _tag_agent(raw: bytes, agent: str) -> tuple[bytes, str]:
    """Stamp the requested agent label and return the normalized event name."""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return raw, ""
    if not isinstance(obj, dict):
        return raw, ""
    # Do not overwrite an explicit agent from the client.
    if not obj.get("agent") and not obj.get("agentName"):
        obj["agent"] = agent
    event_name = str(obj.get("hook_event_name") or obj.get("hookEventName") or "")
    try:
        return json.dumps(obj, ensure_ascii=False).encode("utf-8"), event_name
    except (TypeError, ValueError):
        return raw, event_name


def main() -> int:
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        return 0
    agent = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else "Grok"
    raw, event_name = _tag_agent(raw, agent)
    # Pass through; ai_log_server normalizes camelCase / vendor variants.
    if _post(raw):
        _drain()
    else:
        _spool(raw)
    if agent.casefold() == "codex" and event_name in {"Stop", "SubagentStop"}:
        # Codex requires successful Stop handlers to emit a JSON object.
        sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
