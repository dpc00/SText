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
OUT = r"C:\Users\donal\data\logs\ai"
os.makedirs(OUT, exist_ok=True)

# Daemon-safe: under pythonw (no console) sys.stdout/stderr are None and
# print() raises. Redirect to a devnull so the server never crashes on a
# stray print, and capture uncaught exceptions to a file for debugging.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.path.join(OUT, "server_error.log"), "a")

_lock = threading.Lock()
# session_id -> turn buffer
_sessions = {}


def _date():
    return datetime.date.today().isoformat()


def _ts():
    return datetime.datetime.now()


def _jsonl_path():
    return os.path.join(OUT, f"events_{_date()}.jsonl")


def _md_path():
    return os.path.join(OUT, f"{_date()}.md")


def _append_jsonl(ev, recv_ts):
    rec = {"ts": recv_ts.strftime("%H:%M:%S.%f")[:-3], **ev}
    with open(_jsonl_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _md_header_if_new():
    p = _md_path()
    if not os.path.exists(p):
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"# AI log — {_date()}\n\n")


def _summarize_input(inp):
    if not isinstance(inp, dict) or not inp:
        return ""
    bits = []
    for k, v in inp.items():
        if isinstance(v, str):
            v = v if len(v) <= 80 else v[:77] + "…"
            bits.append(f"{k}={v!r}")
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
            return None
        return _short(ev.get("delta") or "")
    if name == "PostToolBatch":
        calls = ev.get("tool_calls") or []
        names = ", ".join(c.get("tool_name", "?") for c in calls[:6])
        return f"{len(calls)} calls: {names}" if names else f"{len(calls)} calls"
    if name == "Notification":
        m = _short(ev.get("message") or "")
        nt = ev.get("notification_type") or ""
        return f"{m}  ({nt})" if nt and m else (m or (nt or ""))
    # generic: prefer the most informative scalar field present
    for k in ("tool_name", "error", "reason", "cwd", "file", "path",
              "prompt", "message", "source", "subagent_type", "agent_type"):
        v = ev.get(k)
        if isinstance(v, str) and v:
            return _short(v)
    return ""


def _md_ambient_standalone(ts, glyph, name, text):
    _md_header_if_new()
    line = f"### {ts.strftime('%H:%M:%S')}  ◦ {glyph} {name}"
    if text:
        line += f"   {text}"
    with open(_md_path(), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _flush_turn(sid):
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
    items = [(t.get("pre") or start, "tool", t) for t in sess.get("tools", [])]
    items += [(e.get("ts") or start, "extra", e) for e in sess.get("extras", [])]
    items.sort(key=lambda x: x[0])
    for _, kind, it in items:
        if kind == "tool":
            head = f"  ⚙ {it['name']}"
            s = _summarize_input(it.get("input"))
            if s:
                head += f"   {s}"
            pre, post = it.get("pre"), it.get("post")
            if pre and post:
                head += f"   +{(post - pre).total_seconds() * 1000:.0f}ms"
            out.append(head)
            if post:
                out.append(f"  {'✘' if it.get('err') else '✔'} {it['name']}")
            else:
                out.append(f"  ⊘ {it['name']}   (denied / not run)")
        else:
            out.append(f"  {it['glyph']} {it['name']}" + (f"   {it['text']}" if it.get("text") else "").rstrip())
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
    _md_header_if_new()
    with open(_md_path(), "a", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def _mark_tool_done(sid, tool_use_id, name, err):
    s = _sessions.get(sid)
    if not s:
        return
    # pair by tool_use_id (exact; parallel-safe)
    if tool_use_id:
        for t in s["tools"]:
            if t.get("id") == tool_use_id and not t.get("post"):
                t["post"] = _ts()
                t["err"] = err
                return
    # fall back to earliest unmatched same-name tool
    for t in s["tools"]:
        if t["name"] == name and not t.get("post"):
            t["post"] = _ts()
            t["err"] = err
            return


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b""
        try:
            ev = json.loads(body)
        except Exception:
            ev = {"_raw": body.decode("utf-8", "replace")}
        recv = _ts()
        _append_jsonl(ev, recv)
        name = ev.get("hook_event_name", "")
        sid = ev.get("session_id", "_")
        with _lock:
            if name == "UserPromptSubmit":
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
                _mark_tool_done(sid, ev.get("tool_use_id"), ev.get("tool_name", "?"), False)
            elif name == "PostToolUseFailure":
                _mark_tool_done(sid, ev.get("tool_use_id"), ev.get("tool_name", "?"), True)
            elif name == "Stop":
                s = _sessions.get(sid)
                if s:
                    s["stop_ts"] = recv
                    s["stop_msg"] = ev.get("last_assistant_message", "")
                    s["stop_reason"] = ev.get("stop_reason", "")
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
        self.end_headers()
        self.wfile.write(b"{}")


socketserver.TCPServer.allow_reuse_address = True
try:
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), H) as s:
        sys.stdout.write(f"ai_log_server on 127.0.0.1:{PORT} -> {OUT}\n")
        sys.stdout.flush()
        s.serve_forever()
except Exception:
    import traceback
    with open(os.path.join(OUT, "server_error.log"), "a", encoding="utf-8") as f:
        f.write(traceback.format_exc() + "\n")