"""ai_log_server.py — hook-fed conversation logger.

Receives Claude hook events via HTTP POST (Claude itself POSTs each
event here — no JSONL tailing), and writes two formats to ~/data/logs/:

  events_<date>.jsonl   — every event, raw, one JSON line per event (archive / machine)
  <date>.md             — clean human render: turns, collapsed tool calls, final text

This is the "correct" logging path: data straight from Claude's mouth.
"""
import datetime
import json
import os
import sys
import threading
import http.server
import socketserver

PORT = 9511
OUT = r"C:\Users\donal\data\logs"
os.makedirs(OUT, exist_ok=True)

DIAG_DIR = r"C:\Users\donal\data\logs\developer_diagnostics_and_runtime_server_error_logs"
os.makedirs(DIAG_DIR, exist_ok=True)

# Daemon-safe: under pythonw (no console) sys.stdout/stderr are None and
# print() raises. Redirect to a devnull so the server never crashes on a
# stray print, and capture uncaught exceptions to a file for debugging.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.path.join(DIAG_DIR, "server_error.log"), "a", encoding="utf-8")

_lock = threading.Lock()
# session_id -> turn buffer
_sessions = {}


def _date():
    return datetime.date.today().isoformat()


def _ts():
    return datetime.datetime.now()


def _md_path():
    return os.path.join(OUT, f"{_date()}.md")


def _append_jsonl(ev, recv_ts):
    # disabled: user explicitly requested no events_*.jsonl files cluttering their logs directory
    pass


def _md_header_if_new():
    p = _md_path()
    if not os.path.exists(p):
        with open(p, "w", encoding="utf-8", errors="replace") as f:
            f.write(f"# AI log — {_date()}\n\n")


def _map_tool_name(name):
    # Exact Claude Code matches
    mapping = {
        "run_shell_command": "Bash",
        "read_file": "Read",
        "replace": "Edit",
        "write_file": "Write",
        "glob": "Glob",
        "grep_search": "Grep",
        "web_fetch": "WebFetch",
        "google_web_search": "WebSearch",
        "bash": "Bash",
        "read": "Read",
        "edit": "Edit",
        "write": "Write",
    }
    if name in mapping:
        return mapping[name]
    
    # Clean up other MCP tool names
    clean = name
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


def _summarize_event(name, ev):
    """One-line summary of an ambient event's payload, or None to skip the .md line."""
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
        f.write(line + "\n")


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
                            text = json.dumps(resp, ensure_ascii=False)
                        except Exception:
                            text = ""
                    else:
                        text = ""

    # Simple clean up of untrusted_context wrapper tags if present
    text = text.replace("<untrusted_context>\n", "").replace("\n</untrusted_context>", "")
    text = text.replace("<untrusted_context>", "").replace("</untrusted_context>", "")
    text = text.replace("&lt;untrusted_context&gt;\n", "").replace("\n&lt;/untrusted_context&gt;", "")
    text = text.replace("&lt;untrusted_context&gt;", "").replace("&lt;/untrusted_context&gt;", "")
    return text.strip()


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
    # Claude section starts at the earliest tool or ambient event
    ts_cands = []
    if sess.get("first_tool_ts"):
        ts_cands.append(sess["first_tool_ts"])
    for e in sess.get("extras", []):
        if e.get("ts"):
            ts_cands.append(e["ts"])
    claude_ts = min(ts_cands) if ts_cands else start
    out.append(f"### {claude_ts.strftime('%H:%M:%S')}  Claude")
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
        f.write("\n".join(out) + "\n")


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
            recv = _ts()

            # Normalize raw Gemini CLI events
            event_type = ev.get("hook_event_name") or ev.get("event_type")        
            if event_type:
                # Map event types to SText log server names
                if event_type == "BeforeAgent":
                    ev["hook_event_name"] = "UserPromptSubmit"
                    if "prompt" not in ev:
                        ev["prompt"] = ev.get("prompt", "")
                elif event_type == "BeforeTool":
                    ev["hook_event_name"] = "PreToolUse"
                    tool_call = ev.get("tool_call") or {}
                    ev["tool_name"] = ev.get("tool_name") or tool_call.get("name") or "?"
                    ev["tool_input"] = ev.get("tool_input") or tool_call.get("args")
                    ev["tool_use_id"] = ev.get("tool_use_id") or tool_call.get("id")
                elif event_type == "AfterTool":
                    tool_call = ev.get("tool_call") or {}
                    tool_response = ev.get("tool_response") or {}
                    is_error = bool(ev.get("error") or tool_response.get("error"))
                    ev["hook_event_name"] = "PostToolUseFailure" if is_error else "PostToolUse"
                    ev["tool_name"] = ev.get("tool_name") or tool_call.get("name") or "?"
                    ev["tool_use_id"] = ev.get("tool_use_id") or tool_call.get("id")
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
                elif event_type == "Notification":
                    ev["hook_event_name"] = "Notification"
                    ev["message"] = ev.get("message") or ""
                    ev["notification_type"] = ev.get("notification_type") or ""   
                else:
                    ev["hook_event_name"] = event_type

            # Fill session_id
            if "session_id" not in ev:
                ev["session_id"] = ev.get("session_id") or ev.get("session_info", {}).get("session_id") or "_"

            _append_jsonl(ev, recv)
            name = ev.get("hook_event_name", "")
            sid = ev.get("session_id", "_")
            with _lock:
                if name == "UserPromptSubmit":
                    if sid in _sessions:
                        _flush_turn(sid)
                    _sessions[sid] = {
                        "prompt": ev.get("prompt", ""),
                        "start": recv,
                        "tools": [],
                        "extras": [],
                    }
                elif name == "PreToolUse":
                    s = _sessions.setdefault(sid, {"prompt": "", "start": recv, "tools": [], "extras": []})
                    s.setdefault("first_tool_ts", recv)
                    s["tools"].append({
                        "name": ev.get("tool_name", "?"),
                        "input": ev.get("tool_input"),
                        "pre": recv,
                        "id": ev.get("tool_use_id"),
                    })
                elif name == "PostToolUse":
                    _mark_tool_done(sid, ev.get("tool_use_id"), ev.get("tool_name", "?"), False, response=ev.get("tool_response"))
                elif name == "PostToolUseFailure":
                    _mark_tool_done(sid, ev.get("tool_use_id"), ev.get("tool_name", "?"), True, response=ev.get("tool_response"))
                elif name == "AfterModel":
                    s = _sessions.get(sid)
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
                    if s:
                        s["stop_ts"] = recv
                        msg = ev.get("last_assistant_message") or ev.get("prompt_response") or ev.get("message") or ev.get("response") or ""
                        if msg or not s.get("stop_msg"):
                            s["stop_msg"] = msg
                        s["stop_reason"] = ev.get("stop_reason", "")
                    if s and (s.get("stop_msg") or not s.get("tools")):
                        _flush_turn(sid)
                elif name == "SessionEnd":
                    _md_ambient_standalone(recv, "⏹", "SessionEnd", _summarize_event("SessionEnd", ev))
                    _sessions.pop(sid, None)
                else:
                    # ambient event: buffer into the open turn (interleaved by ts),
                    # or write standalone if no turn is currently open
                    text = _summarize_event(name, ev)
                    if text is None:
                        pass  # e.g. non-final MessageDisplay — archived in jsonl only
                    else:
                        s = _sessions.get(sid)
                        if s is not None:
                            s["extras"].append({
                                "ts": recv,
                                "glyph": _GLYPH.get(name, "•"),
                                "name": name,
                                "text": text,
                            })
                        else:
                            _md_ambient_standalone(recv, _GLYPH.get(name, "•"), name, text)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"{}")
        except Exception as e:
            import traceback
            try:
                with open(os.path.join(DIAG_DIR, "post_error.log"), "a", encoding="utf-8") as f:
                    f.write(f"--- POST ERROR: {e} ---\n")
                    traceback.print_exc(file=f)
            except Exception:
                pass
            try:
                self.send_error(500, str(e))
            except Exception:
                pass


def rebuild_from_jsonl(date_str):
    """
    Rebuilds <date_str>.md from events_<date_str>.jsonl by grouping events by session_id,
    reconstructing each session's turns separately, and then merging them chronologically.
    """
    import collections
    jsonl_file = os.path.join(OUT, f"events_{date_str}.jsonl")
    md_file = os.path.join(OUT, f"{date_str}.md")
    
    if not os.path.exists(jsonl_file):
        print(f"No JSONL file found for {date_str} at {jsonl_file}")
        return
        
    print(f"Rebuilding {md_file} from {jsonl_file} by grouping sessions...")
    
    # Read all raw events
    events = []
    with open(jsonl_file, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
                events.append(ev)
            except Exception:
                pass
                
    # Sort globally by ts
    events.sort(key=lambda x: x.get("ts", ""))
    
    # Group events by session_id
    sessions_events = collections.defaultdict(list)
    for ev in events:
        sid = ev.get("session_id", "_")
        sessions_events[sid].append(ev)
        
    # We will reconstruct the turns for each session separately
    all_sessions_markdown = []
    
    for sid, sess_evs in sessions_events.items():
        # Temporary sessions state for this specific sid
        sess_state = {}
        sess_turns = []
        
        # Track agent names/display dynamically for this session
        agent_info = {"name": "Claude", "display": "Claude Code"}
        if sid == "opencode":
            agent_info = {"name": "OpenCode", "display": "OpenCode CLI"}
        elif sid == "openclaw":
            agent_info = {"name": "OpenClaw", "display": "OpenClaw CLI"}
        elif sid == "qwen":
            agent_info = {"name": "Qwen", "display": "Qwen CLI"}
            
        def flush_sess_turn():
            if sid not in sess_state:
                return
            sess = sess_state.pop(sid)
            start = sess.get("start")
            out = []
            out.append(f"### {start.strftime('%H:%M:%S')}  ▸ You")
            if sess.get("prompt"):
                out.append(sess["prompt"])
            out.append("")
            
            ts_cands = []
            if sess.get("first_tool_ts"):
                ts_cands.append(sess["first_tool_ts"])
            for e in sess.get("extras", []):
                if e.get("ts"):
                    ts_cands.append(e["ts"])
            claude_ts = min(ts_cands) if ts_cands else start
            
            # Determine agent name based on current session
            agent_name = agent_info["name"]
            out.append(f"### {claude_ts.strftime('%H:%M:%S')}  {agent_name}")
            
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
            
            sess_turns.append((start, "\n".join(out)))

        for ev in sess_evs:
            name = ev.get("hook_event_name", "")
            
            # Check transcript_path to dynamically resolve agent identity
            tpath = ev.get("transcript_path")
            if tpath and isinstance(tpath, str):
                if ".gemini" in tpath:
                    agent_info = {"name": "Gemini", "display": "Gemini CLI"}
                elif ".claude" in tpath:
                    agent_info = {"name": "Claude", "display": "Claude Code"}
                elif ".openclaw" in tpath:
                    agent_info = {"name": "OpenClaw", "display": "OpenClaw CLI"}
                elif ".qwen" in tpath:
                    agent_info = {"name": "Qwen", "display": "Qwen CLI"}
                elif "opencode" in tpath:
                    agent_info = {"name": "OpenCode", "display": "OpenCode CLI"}
            
            ts_str = ev.get("ts", "")
            try:
                h, m, s_part = ts_str.split(":")
                sec, ms = s_part.split(".")
                y, mo, d = map(int, date_str.split("-"))
                recv = datetime.datetime(y, mo, d, int(h), int(m), int(sec), int(ms) * 1000)
            except Exception:
                recv = datetime.datetime.now()
                
            if name == "UserPromptSubmit":
                if sid in sess_state:
                    flush_sess_turn()
                sess_state[sid] = {
                    "prompt": ev.get("prompt", ""),
                    "start": recv,
                    "tools": [],
                    "extras": [],
                }
            elif name == "PreToolUse":
                s = sess_state.setdefault(sid, {"prompt": "", "start": recv, "tools": [], "extras": []})
                s.setdefault("first_tool_ts", recv)
                s["tools"].append({
                    "name": ev.get("tool_name", "?"),
                    "input": ev.get("tool_input"),
                    "pre": recv,
                    "id": ev.get("tool_use_id"),
                })
            elif name == "PostToolUse":
                s = sess_state.get(sid)
                if s:
                    done_ts = recv
                    tool_use_id = ev.get("tool_use_id")
                    tool_name = ev.get("tool_name", "?")
                    matched = False
                    if tool_use_id:
                        for t in s["tools"]:
                            if t.get("id") == tool_use_id and not t.get("post"):
                                t["post"] = done_ts
                                t["err"] = False
                                matched = True
                                break
                    if not matched:
                        for t in s["tools"]:
                            if t["name"] == tool_name and not t.get("post"):
                                t["post"] = done_ts
                                t["err"] = False
                                break
            elif name == "PostToolUseFailure":
                s = sess_state.get(sid)
                if s:
                    done_ts = recv
                    tool_use_id = ev.get("tool_use_id")
                    tool_name = ev.get("tool_name", "?")
                    matched = False
                    if tool_use_id:
                        for t in s["tools"]:
                            if t.get("id") == tool_use_id and not t.get("post"):
                                t["post"] = done_ts
                                t["err"] = True
                                matched = True
                                break
                    if not matched:
                        for t in s["tools"]:
                            if t["name"] == tool_name and not t.get("post"):
                                t["post"] = done_ts
                                t["err"] = True
                                break
            elif name == "Stop":
                s = sess_state.get(sid)
                if s:
                    s["stop_ts"] = recv
                    msg = ev.get("last_assistant_message") or ev.get("prompt_response") or ev.get("message") or ev.get("response") or ""
                    if msg or not s.get("stop_msg"):
                        s["stop_msg"] = msg
                    s["stop_reason"] = ev.get("stop_reason", "")
                if s and (s.get("stop_msg") or not s.get("tools")):
                    flush_sess_turn()
            elif name == "SessionEnd":
                line = f"### {recv.strftime('%H:%M:%S')}  ◦ ⏹ SessionEnd"
                text = _summarize_event("SessionEnd", ev)
                if text:
                    line += f"   {text}"
                sess_turns.append((recv, line + "\n"))
                sess_state.pop(sid, None)
            else:
                text = _summarize_event(name, ev)
                if text is not None:
                    s = sess_state.get(sid)
                    if s is not None:
                        s["extras"].append({
                            "ts": recv,
                            "glyph": _GLYPH.get(name, "•"),
                            "name": name,
                            "text": text,
                        })
                    else:
                        line = f"### {recv.strftime('%H:%M:%S')}  ◦ {_GLYPH.get(name, '•')} {name}"
                        if text:
                            line += f"   {text}"
                        sess_turns.append((recv, line + "\n"))
                        
        if sid in sess_state:
            flush_sess_turn()
            
        if sess_turns:
            sess_turns.sort(key=lambda x: x[0])
            session_start_time = sess_turns[0][0]
            
            agent_display = agent_info["display"]
            sess_md = []
            sess_md.append(f"## ─── Session Start — {agent_display} ({sid}) ───")
            sess_md.append("")
            for _, turn_text in sess_turns:
                sess_md.append(turn_text)
            sess_md.append(f"## ─── Session End — {agent_display} ───\n")
            
            all_sessions_markdown.append((session_start_time, "\n".join(sess_md)))
            
    all_sessions_markdown.sort(key=lambda x: x[0])
    
    if os.path.exists(md_file):
        os.remove(md_file)
        
    with open(md_file, "w", encoding="utf-8", errors="replace") as f:
        f.write(f"# AI log — {date_str}\n\n")
        for _, sess_md_text in all_sessions_markdown:
            f.write(sess_md_text + "\n")
            
    print(f"Rebuild completed successfully for {date_str}! All CLI sessions separated and restored!")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", help="Rebuild markdown log for a specific date (YYYY-MM-DD)")
    parser.add_argument("--port", type=int, default=PORT, help="Port to listen on")
    args = parser.parse_args()
    
    if args.rebuild:
        rebuild_from_jsonl(args.rebuild)
        sys.exit(0)
        
    socketserver.TCPServer.allow_reuse_address = True
    try:
        with socketserver.ThreadingTCPServer(("127.0.0.1", args.port), H) as s:
            sys.stdout.write(f"ai_log_server on 127.0.0.1:{args.port} -> {OUT}\n")
            sys.stdout.flush()
            s.serve_forever()
    except Exception:
        import traceback
        with open(os.path.join(DIAG_DIR, "server_error.log"), "a", encoding="utf-8") as f:
            f.write(traceback.format_exc() + "\n")