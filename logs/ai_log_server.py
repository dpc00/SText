"""ai_log_server.py — hook-fed conversation logger.

Receives agent hook events via HTTP POST (Claude Code, Gemini, opencode
POST each event here), and writes a clean human-readable markdown log
to ~/data/logs/<date>.md: turns, collapsed tool calls, final text.

This is the "correct" logging path: data straight from the agent's mouth.
"""
import datetime
import json
import os
import re
import sys
import time
import threading
import http.server
import socketserver

PORT = 9511
OUT = r"C:\Users\donal\data\logs"
os.makedirs(OUT, exist_ok=True)

DIAG_DIR = r"C:\Users\donal\data\logs\developer_diagnostics_and_runtime_server_error_logs"
os.makedirs(DIAG_DIR, exist_ok=True)

# Daemon-safe I/O: PluginLoader spawns us with stdout/stderr=DEVNULL (or
# pythonw leaves them None). print()/flush() on a broken or NUL handle has
# been seen to raise OSError [Errno 22] Invalid argument and kill the whole
# process — which is exactly the "log halted for no reason" failure mode.
#
# Only rebind when *running as the server* (__main__). Importing this module
# from SublimeREPL / tests / tools must not steal the caller's stdio (that
# made REPL print() vanish into DIAG_DIR after `import ai_log_server`).
def _isatty(stream):
    try:
        return stream is not None and hasattr(stream, "isatty") and stream.isatty()
    except Exception:
        return False


def _rebind_stdio():
    err_path = os.path.join(DIAG_DIR, "server_error.log")
    out_path = os.path.join(DIAG_DIR, "server_runtime.log")
    if not _isatty(sys.stderr):
        try:
            sys.stderr = open(err_path, "a", encoding="utf-8", errors="replace")
        except OSError:
            pass
    if not _isatty(sys.stdout):
        try:
            sys.stdout = open(out_path, "a", encoding="utf-8", errors="replace")
        except OSError:
            try:
                sys.stdout = open(os.devnull, "w")
            except OSError:
                pass

_lock = threading.Lock()
# session_id -> turn buffer
_sessions = {}
# dedup: (sid, event_name, second-precision ts) seen recently
_recent_events = {}
_DEDUP_TTL = 5.0  # seconds


def _date():
    return datetime.date.today().isoformat()


def _ts():
    return datetime.datetime.now()


def _scrub_utf8(s):
    """Make text safe for UTF-8 file writes.

    Agent payloads can contain lone surrogates (e.g. \\udc9d from binary
    tool output). Python's utf-8 codec refuses those even with
    errors='replace' on the file object — scrub first.
    """
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return s.encode("utf-8", "surrogatepass").decode("utf-8", "replace")


def _scrub_obj(obj):
    """Recursively scrub strings in dict/list trees (for json.dumps / archives).

    Lone surrogates survive json.dumps(ensure_ascii=False) and only blow up
    when the resulting line is encoded to UTF-8 for disk — which used to
    kill _append_jsonl and leave holes in the daily log pipeline.
    """
    if isinstance(obj, str):
        return _scrub_utf8(obj)
    if isinstance(obj, dict):
        return {(_scrub_utf8(k) if isinstance(k, str) else k): _scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub_obj(v) for v in obj]
    return obj


def _safe_json_dumps(obj, **kwargs):
    """json.dumps after surrogate scrub; never raises on bad Unicode."""
    kwargs.setdefault("ensure_ascii", False)
    try:
        return json.dumps(_scrub_obj(obj), **kwargs)
    except (TypeError, ValueError):
        try:
            return json.dumps(_scrub_obj(obj), ensure_ascii=True, default=str)
        except Exception:
            return "{}"


def _md_path():
    return os.path.join(OUT, f"{_date()}.md")


def _md_header_if_new():
    p = _md_path()
    if not os.path.exists(p):
        with open(p, "w", encoding="utf-8", errors="replace") as f:
            f.write(f"# AI log — {_date()}\n\n")


def _map_tool_name(name):
    # Claude Code + Grok Build + common aliases (see ~/.grok/docs hooks)
    mapping = {
        "run_shell_command": "Bash",
        "run_terminal_command": "Bash",
        "read_file": "Read",
        "replace": "Edit",
        "search_replace": "Edit",
        "write_file": "Write",
        "write": "Write",
        "glob": "Glob",
        "list_dir": "Glob",
        "grep_search": "Grep",
        "grep": "Grep",
        "web_fetch": "WebFetch",
        "web_search": "WebSearch",
        "google_web_search": "WebSearch",
        "spawn_subagent": "Task",
        "bash": "Bash",
        "read": "Read",
        "edit": "Edit",
    }
    if not name:
        return "?"
    key = str(name)
    if key in mapping:
        return mapping[key]
    # Case-insensitive / CamelCase (RunTerminalCommand → run_terminal_command)
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).replace("-", "_").lower()
    if snake in mapping:
        return mapping[snake]
    low = key.lower()
    if low in mapping:
        return mapping[low]

    # Clean up other MCP tool names
    clean = key
    if clean.startswith("mcp_"):
        clean = clean[4:]
    if clean.startswith("sublime-mcp_"):
        clean = clean[12:]
    elif clean.startswith("computer-use-mcp_"):
        clean = clean[17:]
    elif clean.startswith("firecrawl_"):
        clean = clean[10:]
    elif clean.startswith("github_"):
        clean = clean[7:]

    # Convert to a nice CamelCase or clean capitalization
    parts = clean.replace("-", "_").split("_")
    return "".join(p.capitalize() for p in parts if p)


def _summarize_tool_input(name, inp):
    if not isinstance(inp, dict) or not inp:
        return ""
    
    mapped_name = _map_tool_name(name)
    
    # 2. Extract primary scalar fields based on tool style
    if mapped_name == "Bash":
        cmd = (inp.get("command") or inp.get("shell_cmd") or "").strip().replace("\n", "; ")
        return f"command={cmd[:120]!r}" if len(cmd) > 120 else f"command={cmd!r}"
        
    if mapped_name in ("Read", "Edit", "Write"):
        path = inp.get("file_path") or inp.get("filePath") or inp.get("path") or ""
        return f"file_path={path!r}" if path else ""
        
    if mapped_name in ("Glob", "Grep"):
        pat = inp.get("pattern") or ""
        path = inp.get("path") or inp.get("dir_path") or ""
        out = f"pattern={pat!r}"
        if path:
            out += f"  path={path!r}"
        return out
        
    if mapped_name == "WebSearch":
        q = inp.get("query") or ""
        return f"query={q!r}" if q else ""
        
    if mapped_name == "WebFetch":
        url = inp.get("url") or inp.get("prompt") or ""
        return f"url={url!r}" if url else ""

    # Generic fallback: print all key-values excluding huge ones
    bits = []
    # prioritize common diagnostic keys
    keys = sorted(inp.keys(), key=lambda k: 0 if k in ("path", "filePath", "file_path", "pattern", "code", "command", "text", "query", "url") else 1)
    for k in keys:
        v = inp[k]
        if isinstance(v, str):
            v_clean = v.replace("\n", "; ")
            if len(v_clean) > 80:
                v_clean = v_clean[:77] + "…"
            bits.append(f"{k}={v_clean!r}")
        elif isinstance(v, (int, float, bool)):
            bits.append(f"{k}={v}")
        elif isinstance(v, list):
            bits.append(f"{k}=[{len(v)}]")
        elif isinstance(v, dict):
            bits.append(f"{k}={{{len(v)}}}")
        else:
            bits.append(f"{k}=…")
    return "  ".join(bits)


# Ambient events (everything that isn't part of the turn skeleton
# UserPromptSubmit/PreToolUse/PostToolUse/PostToolUseFailure/Stop) get a
# glyph + one-line summary and are interleaved into the turn by timestamp,
# or written as a standalone line if no turn is open.
_GLYPH = {
    "SessionStart":        "▶",
    "Setup":               "⚙",
    "UserPromptExpansion": "▸",
    "PermissionRequest":   "🔐",
    "PermissionDenied":    "🚫",
    "PostToolBatch":       "📦",
    "Notification":        "🔔",
    "MessageDisplay":      "💬",
    "SubagentStart":       "▶",
    "SubagentStop":        "◀",
    "TaskCreated":         "＋",
    "TaskCompleted":       "✓",
    "StopFailure":         "✘",
    "TeammateIdle":        "⏸",
    "InstructionsLoaded":  "📋",
    "ConfigChange":        "⚙",
    "CwdChanged":          "📂",
    "FileChanged":         "📝",
    "WorktreeCreate":      "🌳+",
    "WorktreeRemove":      "🌳-",
    "PreCompact":          "🧹",
    "PostCompact":         "🧹",
    "Elicitation":         "❓",
    "ElicitationResult":   "❓✔",
    "SessionEnd":          "⏹",
}


def _short(s, n=100):
    if not isinstance(s, str):
        return ""
    return s if len(s) <= n else s[:n - 3] + "…"


def _normalize_agent_label(v):
    """Map vendor/source strings to a short display label."""
    if not isinstance(v, str):
        return None
    raw = v.strip()
    if not raw:
        return None
    low = raw.lower().replace(" ", "-").replace("_", "-")
    mapping = {
        "grok": "Grok",
        "grok-build": "Grok",
        "grok-cli": "Grok",
        "xai": "Grok",
        "claude": "Claude",
        "claude-code": "Claude",
        "anthropic": "Claude",
        "gemini": "Gemini",
        "gemini-cli": "Gemini",
        "cursor": "Cursor",
        "codex": "Codex",
        "opencode": "OpenCode",
    }
    if low in mapping:
        return mapping[low]
    # Keep short custom labels; truncate long garbage.
    return raw[:32] if len(raw) <= 32 else raw[:29] + "…"


def _detect_agent(ev):
    """Best-effort agent label from envelope fields or vendor event shapes."""
    if not isinstance(ev, dict):
        return None
    for k in (
        "agent",
        "agent_name",
        "agentName",
        "client",
        "client_name",
        "clientName",
        "source",
        "app",
        "vendor",
    ):
        label = _normalize_agent_label(ev.get(k))
        if label:
            return label
    # Gemini CLI uses BeforeAgent / AfterTool / etc.
    et = ev.get("hook_event_name") or ev.get("event_type") or ""
    if et in ("BeforeAgent", "AfterAgent", "BeforeTool", "AfterTool",
              "BeforeModel", "AfterModel", "BeforeToolSelection"):
        return "Gemini"
    return None


def _is_permission_noise(name, ev):
    """True for tool-permission prompts that spam the daily log."""
    if name in ("PermissionRequest",):
        return True
    if name != "Notification":
        return False
    nt = str(ev.get("notification_type") or ev.get("notificationType") or "").lower()
    if nt in (
        "permission_prompt",
        "permission-prompt",
        "permissionprompt",
        "permission_request",
        "permission-request",
        "permissionrequest",
    ):
        return True
    msg = str(ev.get("message") or "").lower()
    if "permission" in msg and ("request" in msg or "prompt" in msg):
        return True
    return False


def _summarize_event(name, ev):
    """One-line summary of an ambient event's payload, or None to skip the .md line."""
    if _is_permission_noise(name, ev):
        return None
    if name == "MessageDisplay":
        # skip streaming chunks; render only the final per-message delta
        if not ev.get("final"):
            return _short(ev.get("delta") or "") + " [chunk]"
        return _short(ev.get("delta") or "")
    if name == "PostToolBatch":
        calls = ev.get("tool_calls") or []
        names = ", ".join(c.get("tool_name", "?") for c in calls[:6])
        return f"{len(calls)} calls: {names}" if names else f"{len(calls)} calls"
    if name == "Notification":
        m = _short(ev.get("message") or "")
        nt = ev.get("notification_type") or ""
        return f"{m}  ({nt})" if nt and m else (m or (nt or ""))
    
    # Custom summaries for model-level events
    if name == "BeforeModel":
        model = ev.get("model") or ev.get("model_name") or ""
        if model:
            return f"Model: {model}"
        return "Preparing model request"
    if name == "AfterModel":
        resp = ev.get("llm_response") or {}
        candidates = resp.get("candidates") or []
        if candidates:
            return f"{len(candidates)} candidates generated"
        return "Model response received"
    if name == "BeforeToolSelection":
        return "Evaluating tool selection"
    if name == "PreCompress":
        return "Preparing context compression"

    # generic: prefer the most informative scalar field present
    for k in ("tool_name", "error", "reason", "cwd", "file", "path",
              "prompt", "message", "source", "subagent_type", "agent_type"):
        v = ev.get(k)
        if isinstance(v, str) and v:
            return _short(v)
    return ""


def _md_ambient_standalone(ts, glyph, name, text, path=None):
    if not path:
        _md_header_if_new()
        path = _md_path()
    line = f"### {ts.strftime('%H:%M:%S')}  ◦ {glyph} {name}"
    if text:
        line += f"   {text}"
    with open(path, "a", encoding="utf-8", errors="replace") as f:
        f.write(_scrub_utf8(line) + "\n")


def _format_tool_response(resp):
    if not resp:
        return ""
    if isinstance(resp, str):
        text = resp
    elif not isinstance(resp, dict):
        text = str(resp)
    else:
        # Check for direct stdout / output keys (which are clean strings)
        found_str = None
        for k in ("stdout", "output", "stderr"):
            val = resp.get(k)
            if val and isinstance(val, str):
                found_str = val
                break
        if found_str is not None:
            text = found_str
        else:
            # Check llmContent blocks (contains clean text sent to LLM)
            content = resp.get("llmContent")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                text = "\n".join(text_parts) if text_parts else ""
            else:
                # Check returnDisplay (contains raw text cells shown on screen)
                ret = resp.get("returnDisplay")
                if isinstance(ret, str):
                    text = ret
                elif isinstance(ret, list):
                    # returnDisplay is list of lists of dicts, or list of dicts
                    text_parts = []
                    for row in ret:
                        if isinstance(row, list):
                            row_text = ""
                            for cell in row:
                                if isinstance(cell, dict) and "text" in cell:
                                    row_text += cell.get("text", "")
                                elif isinstance(cell, str):
                                    row_text += cell
                            text_parts.append(row_text)
                        elif isinstance(row, dict) and "text" in row:
                            text_parts.append(row.get("text", ""))
                        elif isinstance(row, str):
                            text_parts.append(row)
                    text = "\n".join(text_parts) if text_parts else ""
                else:
                    # If it's a small dictionary (no huge lists), we can format it as inline JSON
                    has_huge_lists = False
                    for val in resp.values():
                        if isinstance(val, list) and len(val) > 10:
                            has_huge_lists = True
                            break
                    if not has_huge_lists:
                        try:
                            text = _safe_json_dumps(resp)
                        except Exception:
                            text = ""
                    else:
                        text = ""

    # Simple clean up of untrusted_context wrapper tags if present
    text = text.replace("<untrusted_context>\n", "").replace("\n</untrusted_context>", "")
    text = text.replace("<untrusted_context>", "").replace("</untrusted_context>", "")
    text = text.replace("&lt;untrusted_context&gt;\n", "").replace("\n&lt;/untrusted_context&gt;", "")
    text = text.replace("&lt;untrusted_context&gt;", "").replace("&lt;/untrusted_context&gt;", "")
    return _scrub_utf8(text).strip()


def _flush_turn(sid, path=None):
    sess = _sessions.pop(sid, None)
    if not sess:
        return
    start = sess.get("start")
    out = []
    out.append(f"### {start.strftime('%H:%M:%S')}  ▸ You")
    if sess.get("prompt"):
        out.append(sess["prompt"])
    out.append("")
    # Agent section starts at the earliest tool or ambient event
    ts_cands = []
    if sess.get("first_tool_ts"):
        ts_cands.append(sess["first_tool_ts"])
    for e in sess.get("extras", []):
        if e.get("ts"):
            ts_cands.append(e["ts"])
    agent_ts = min(ts_cands) if ts_cands else start
    # Default Claude: historical HTTP hooks from Claude Code omit agent.
    # Grok's command forwarder injects agent="Grok".
    agent_label = sess.get("agent") or "Claude"
    out.append(f"### {agent_ts.strftime('%H:%M:%S')}  {agent_label}")
    # merge tool calls and ambient extras by timestamp so the log is chronological
    tools = sess.get("tools", [])
    extras = sess.get("extras", [])
    
    if tools:
        from collections import Counter
        counts = Counter()
        denied = 0
        failed = 0
        for t in tools:
            tname = _map_tool_name(t["name"])
            if not t.get("post"):
                denied += 1
            elif t.get("err"):
                failed += 1
            counts[tname] += 1
        parts = [f"{count}x {name}" for name, count in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
        summary = "  ⚙ Tools: " + ", ".join(parts)
        extra_info = []
        if failed:
            extra_info.append(f"{failed} failed")
        if denied:
            extra_info.append(f"{denied} denied")
        if extra_info:
            summary += f"  ({'; '.join(extra_info)})"
        out.append(summary)

    # Chronologically print only extras
    extras.sort(key=lambda x: x.get("ts") or start)
    for e in extras:
        out.append(f"  {e['glyph']} {e['name']}" + (f"   {e['text']}" if e.get("text") else "").rstrip())
    if sess.get("stop_msg"):
        thinking = sess.get("thinking", [])
        if thinking:
            out.append("")
            out.append("> **Thinking Process:**")
            for t in thinking:
                for line in t.split("\n"):
                    out.append(f"> {line}" if line.strip() else ">")
            out.append("")
        out.append("")
        out.append(sess["stop_msg"])
    out.append("")
    foot = []
    stop_ts = sess.get("stop_ts")
    if stop_ts and start:
        foot.append(f"{(stop_ts - start).total_seconds():.1f}s")
    if sess.get("stop_reason") and sess["stop_reason"] != "end_turn":
        foot.append(sess["stop_reason"])
    if foot:
        out.append("  — " + "  ·  ".join(foot))
        out.append("")
    if not path:
        _md_header_if_new()
        path = _md_path()
    with open(path, "a", encoding="utf-8", errors="replace") as f:
        f.write(_scrub_utf8("\n".join(out)) + "\n")


def _mark_tool_done(sid, tool_use_id, name, err, ts=None, response=None):
    s = _sessions.get(sid)
    if not s:
        return
    done_ts = ts or _ts()
    # pair by tool_use_id (exact; parallel-safe)
    if tool_use_id:
        for t in s["tools"]:
            if t.get("id") == tool_use_id and not t.get("post"):
                t["post"] = done_ts
                t["err"] = err
                if response:
                    t["response"] = response
                return
    # fall back to earliest unmatched same-name tool
    for t in s["tools"]:
        if t["name"] == name and not t.get("post"):
            t["post"] = done_ts
            t["err"] = err
            if response:
                t["response"] = response
            return


# Grok CLI posts camelCase envelopes (hookEventName, sessionId, …). Claude Code
# and our historical clients use snake_case. Map known aliases once so handlers
# only read snake_case keys.
_CAMEL_TO_SNAKE = {
    "hookEventName": "hook_event_name",
    "eventType": "event_type",
    "sessionId": "session_id",
    "toolName": "tool_name",
    "toolInput": "tool_input",
    "toolUseId": "tool_use_id",
    "toolResponse": "tool_response",
    "toolResult": "tool_response",  # Grok PostToolUse field (not Claude's tool_response)
    "lastAssistantMessage": "last_assistant_message",
    "stopReason": "stop_reason",
    "notificationType": "notification_type",
    "permissionMode": "permission_mode",
    "workspaceRoot": "workspace_root",
    "userPrompt": "prompt",
    "promptText": "prompt",
    "stopHookActive": "stop_hook_active",
    "agentName": "agent",
    "clientName": "client",
}


def _coerce_text(val):
    """Flatten prompt/message fields that may be str or content-block lists."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        parts = []
        for block in val:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                t = block.get("text") or block.get("content") or ""
                if isinstance(t, str) and t:
                    parts.append(t)
        return "\n".join(parts)
    if isinstance(val, dict):
        t = val.get("text") or val.get("content") or val.get("prompt") or ""
        return t if isinstance(t, str) else ""
    return str(val)


def _extract_prompt(ev):
    """Best-effort user prompt from Claude / Grok / Cursor envelopes."""
    if not isinstance(ev, dict):
        return ""
    for key in (
        "prompt",
        "user_prompt",
        "userPrompt",
        "prompt_text",
        "promptText",
        "text",
        "message",
        "content",
        "input",
    ):
        if key in ev:
            text = _coerce_text(ev.get(key))
            if text.strip():
                return text
    return ""


def _normalize_event_keys(ev):
    """Copy camelCase Grok fields onto snake_case aliases (in place)."""
    if not isinstance(ev, dict):
        return ev
    for camel, snake in _CAMEL_TO_SNAKE.items():
        if camel in ev and snake not in ev:
            ev[snake] = ev[camel]
    # Nested session info sometimes carries the id only.
    if not ev.get("session_id"):
        info = ev.get("session_info") or ev.get("sessionInfo") or {}
        if isinstance(info, dict):
            ev["session_id"] = (
                info.get("session_id")
                or info.get("sessionId")
                or ev.get("sessionId")
                or "_"
            )
    # Grok UserPromptSubmit may not use Claude's plain `prompt` key.
    if not _coerce_text(ev.get("prompt")).strip():
        extracted = _extract_prompt(ev)
        if extracted:
            ev["prompt"] = extracted
    # Prefer tool_response; Grok only sends toolResult (mapped above).
    if "tool_response" not in ev and "tool_result" in ev:
        ev["tool_response"] = ev["tool_result"]
    return ev


def process_event(ev, recv=None):
    """Ingest one normalized hook event into the session/turn buffers.

    Shared by the HTTP handler (do_POST) and the in-process journal tailer
    (jcode_tailer) so both paths reuse the exact same turn-flush logic.
    Returns None; all output goes to the daily markdown log.
    """
    if recv is None:
        recv = _ts()
    _normalize_event_keys(ev)

    # Normalize raw Gemini CLI events
    event_type = ev.get("hook_event_name") or ev.get("event_type")
    if event_type:
        # Map event types to SText log server names
        if event_type == "BeforeAgent":
            ev["hook_event_name"] = "UserPromptSubmit"
            if "prompt" not in ev:
                ev["prompt"] = ev.get("prompt", "")
            ev.setdefault("agent", "Gemini")
        elif event_type == "BeforeTool":
            ev["hook_event_name"] = "PreToolUse"
            tool_call = ev.get("tool_call") or {}
            ev["tool_name"] = ev.get("tool_name") or tool_call.get("name") or "?"
            ev["tool_input"] = ev.get("tool_input") or tool_call.get("args")
            ev["tool_use_id"] = ev.get("tool_use_id") or tool_call.get("id")
            ev.setdefault("agent", "Gemini")
        elif event_type == "AfterTool":
            tool_call = ev.get("tool_call") or {}
            tool_response = ev.get("tool_response") or {}
            is_error = bool(ev.get("error") or tool_response.get("error"))
            ev["hook_event_name"] = "PostToolUseFailure" if is_error else "PostToolUse"
            ev["tool_name"] = ev.get("tool_name") or tool_call.get("name") or "?"
            ev["tool_use_id"] = ev.get("tool_use_id") or tool_call.get("id")
            ev.setdefault("agent", "Gemini")
        elif event_type == "AfterAgent":
            ev["hook_event_name"] = "Stop"
            ev["last_assistant_message"] = (
                ev.get("last_assistant_message")
                or ev.get("prompt_response")
                or ev.get("response")
                or ev.get("message")
                or ""
            )
            ev["stop_reason"] = ev.get("stop_reason") or ""
            ev.setdefault("agent", "Gemini")
        elif event_type == "Notification":
            ev["hook_event_name"] = "Notification"
            ev["message"] = ev.get("message") or ""
            ev["notification_type"] = ev.get("notification_type") or ""
        else:
            # Grok/Claude event names already match; lower-case variants too.
            if event_type and event_type[0].islower():
                # e.g. "user_prompt_submit" / "stop" from some runners
                parts = event_type.replace("-", "_").split("_")
                event_type = "".join(p[:1].upper() + p[1:] for p in parts if p)
            ev["hook_event_name"] = event_type

    # Fill session_id
    if not ev.get("session_id"):
        ev["session_id"] = (
            ev.get("sessionId")
            or (ev.get("session_info") or {}).get("session_id")
            or "_"
        )

    name = ev.get("hook_event_name", "")
    sid = ev.get("session_id", "_")
    agent = _detect_agent(ev)
    # Drop permission-prompt spam early.
    if name in ("Notification", "PermissionRequest") and _is_permission_noise(name, ev):
        return
    # Dedup: drop identical ambient events (same sid+name within 1s).
    # Tool events (PreToolUse/PostToolUse) are NOT deduped -- they carry
    # unique tool_use_ids and parallel calls can share a name+second.
    if name in ("InstructionsLoaded", "SessionEnd", "SessionStart",
                "Setup", "Notification", "ConfigChange", "CwdChanged"):
        dedup_key = (sid, name, recv.strftime("%Y%m%d%H%M%S"))
        now = time.time()
        with _lock:
            last_ts = _recent_events.get(dedup_key)
            if last_ts and (now - last_ts) < _DEDUP_TTL:
                return
            _recent_events[dedup_key] = now
    with _lock:
        def _touch_agent(s):
            if agent and s is not None and not s.get("agent"):
                s["agent"] = agent

        if name == "UserPromptSubmit":
            if sid in _sessions:
                _flush_turn(sid)
            _sessions[sid] = {
                "prompt": _extract_prompt(ev) or _coerce_text(ev.get("prompt")),
                "start": recv,
                "tools": [],
                "extras": [],
                "agent": agent or "Claude",
            }
        elif name == "PreToolUse":
            s = _sessions.setdefault(
                sid,
                {"prompt": "", "start": recv, "tools": [], "extras": [], "agent": agent or "Claude"},
            )
            _touch_agent(s)
            s.setdefault("first_tool_ts", recv)
            s["tools"].append({
                "name": ev.get("tool_name", "?"),
                "input": ev.get("tool_input"),
                "pre": recv,
                "id": ev.get("tool_use_id"),
            })
        elif name == "PostToolUse":
            s = _sessions.get(sid)
            _touch_agent(s)
            _mark_tool_done(sid, ev.get("tool_use_id"), ev.get("tool_name", "?"), False, response=ev.get("tool_response"))
        elif name == "PostToolUseFailure":
            s = _sessions.get(sid)
            _touch_agent(s)
            _mark_tool_done(sid, ev.get("tool_use_id"), ev.get("tool_name", "?"), True, response=ev.get("tool_response"))
        elif name == "AfterModel":
            s = _sessions.get(sid)
            _touch_agent(s)
            if s is not None:
                resp = ev.get("llm_response") or {}
                try:
                    parts = resp.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                    for p in parts:
                        text = ""
                        if isinstance(p, dict) and "thought" in p:        
                            text = p.get("text", "")
                        elif isinstance(p, str) and ("**Analyzing" in p or "**Checking" in p or "**Refining" in p or "**Investigating" in p or "**Observing" in p or "**Clarifying" in p):
                            text = p
                        if text:
                            current = s.setdefault("thinking", [])        
                            if not current:
                                current.append(text)
                            else:
                                if text.startswith(current[-1]):
                                    current[-1] = text
                                elif not current[-1].startswith(text):    
                                    current.append(text)
                except Exception:
                    pass
        elif name == "Stop":
            s = _sessions.get(sid)
            _touch_agent(s)
            if s:
                s["stop_ts"] = recv
                msg = ev.get("last_assistant_message") or ev.get("prompt_response") or ev.get("message") or ev.get("response") or ""
                if msg or not s.get("stop_msg"):
                    s["stop_msg"] = msg
                s["stop_reason"] = ev.get("stop_reason", "")
            # defer_flush: set the closing message but keep the turn open
            # (jcode narrates mid-turn; the real boundary is the next
            # user prompt or an idle timeout). force_flush closes now.
            if ev.get("defer_flush") and not ev.get("force_flush"):
                pass
            elif s and (ev.get("force_flush") or s.get("stop_msg") or not s.get("tools")):
                _flush_turn(sid)
        elif name == "SessionEnd":
            _md_ambient_standalone(recv, "⏹", "SessionEnd", _summarize_event("SessionEnd", ev))
            _sessions.pop(sid, None)
        else:
            # ambient event: buffer into the open turn (interleaved by ts),
            # or write standalone if no turn is currently open
            text = _summarize_event(name, ev)
            if text is None:
                pass  # e.g. non-final MessageDisplay / permission noise
            else:
                s = _sessions.get(sid)
                _touch_agent(s)
                if s is not None:
                    s["extras"].append({
                        "ts": recv,
                        "glyph": _GLYPH.get(name, "•"),
                        "name": name,
                        "text": text,
                    })
                else:
                    _md_ambient_standalone(recv, _GLYPH.get(name, "•"), name, text)


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n) if n else b""
            try:
                ev = json.loads(body)
            except Exception:
                ev = {"_raw": body.decode("utf-8", "replace")}
            process_event(ev)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"{}")
        except Exception as e:
            import traceback
            try:
                with open(
                    os.path.join(DIAG_DIR, "post_error.log"),
                    "a",
                    encoding="utf-8",
                    errors="replace",
                ) as f:
                    f.write(_scrub_utf8(f"--- POST ERROR: {e} ---\n"))
                    f.write(_scrub_utf8(traceback.format_exc() + "\n"))
            except Exception:
                pass
            try:
                self.send_error(500, _scrub_utf8(str(e)))
            except Exception:
                pass


def _safe_log(msg):
    """Best-effort banner/heartbeat; never raise (stdout death used to abort serve)."""
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n"
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None:
                continue
            stream.write(line)
            stream.flush()
            return
        except Exception:
            continue
    try:
        with open(os.path.join(DIAG_DIR, "server_runtime.log"), "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
    except OSError:
        pass


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PORT, help="Port to listen on")
    args = parser.parse_args()

    # Rebind again after import in case parent closed inherited handles.
    _rebind_stdio()

    # jcode has no native HTTP hooks; instead of a per-event spawned script we
    # tail its append-only session journals from one background thread and feed
    # them through the same process_event() sink as HTTP hook clients.
    try:
        import jcode_tailer
        jcode_tailer.start(process_event)
        _safe_log("jcode_tailer started (journal -> log)")
    except Exception:
        import traceback
        try:
            with open(os.path.join(DIAG_DIR, "server_error.log"), "a", encoding="utf-8", errors="replace") as f:
                f.write("jcode_tailer failed to start:\n" + traceback.format_exc() + "\n")
        except OSError:
            pass

    socketserver.TCPServer.allow_reuse_address = True
    try:
        with socketserver.ThreadingTCPServer(("0.0.0.0", args.port), H) as s:
            _safe_log(f"ai_log_server listening 0.0.0.0:{args.port} pid={os.getpid()} -> {OUT}")
            s.serve_forever()
    except Exception:
        import traceback
        try:
            with open(os.path.join(DIAG_DIR, "server_error.log"), "a", encoding="utf-8", errors="replace") as f:
                f.write(traceback.format_exc() + "\n")
        except OSError:
            pass
        # Re-raise only after logging so a failed bind is visible to the spawner.
        raise SystemExit(1)
