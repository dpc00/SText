"""rebuild_ai_log.py — Master Multi-CLI Log Rebuilder.

Reconstructs pristine daily Markdown logs (<date>.md) under ~/data/logs/ai/
by reading the authoritative session JSONL files for ALL running CLIs:
  - Claude Code (from ~/.claude/projects/*)
  - Gemini CLI (from ~/.gemini/tmp/stext/chats)
  - OpenClaw CLI (from ~/.openclaw/agents/main/sessions)
  - OpenCode CLI (from ~/data/logs/ai/events_*.jsonl)
  - Codex CLI (from ~/.codex/sessions/YYYY/MM/DD)

This creates a complete, un-scrambled, disjoint, chronological record of your
daily research work across all agent tools, with absolutely NO clutter or tool stdout.
"""

import os
import sys
import json
import datetime
import collections

OUT = os.path.expanduser("~/data/logs/ai")

_GLYPH = {
    "SessionStart":        "▶",
    "Setup":               "⚙",
    "UserPromptExpansion": "🔍",
    "PermissionRequest":   "🔒",
    "PermissionDenied":    "🚫",
    "PostToolBatch":       "📦",
    "Notification":        "🔔",
    "MessageDisplay":      "💬",
    "SubagentStart":       "▶",
    "SubagentStop":        "◀",
    "TaskCreated":         "＋",
    "TaskCompleted":       "✓",
    "StopFailure":         "⚠️",
    "TeammateIdle":        "🔔",
    "InstructionsLoaded":  "📚",
    "ConfigChange":        "🔧",
    "CwdChanged":          "📂",
    "FileChanged":         "📝",
    "WorktreeCreate":      "🌳+",
    "WorktreeRemove":      "🌳-",
    "PreCompact":          "🧹",
    "PostCompact":         "🧹",
    "Elicitation":         "❓",
    "ElicitationResult":   "📝",
    "SessionEnd":          "⏹",
}


def parse_iso_datetime(s):
    # s can be e.g. "2026-07-10T02:48:36.879Z" or "2026-07-10 02:48:58"
    s = s.replace("Z", "")
    if "T" in s:
        parts = s.split("T")
        date_part = parts[0]
        time_part = parts[1]
    else:
        parts = s.split(" ")
        date_part = parts[0]
        time_part = parts[1]
        
    y, mo, d = map(int, date_part.split("-"))
    time_subparts = time_part.split(":")
    h = int(time_subparts[0])
    m = int(time_subparts[1])
    sec_parts = time_subparts[2].split(".")
    sec = int(sec_parts[0])
    ms = int(sec_parts[1]) if len(sec_parts) > 1 else 0
    if ms > 0:
        ms_str = f"{ms:03d}"[:6]
        ms = int(ms_str.ljust(6, "0"))
    return datetime.datetime(y, mo, d, h, m, sec, ms)


def parse_time_with_date(ts_str, date_str):
    # ts_str is e.g. "05:43:07.573"
    h, m, s_part = ts_str.split(":")
    sec, ms = s_part.split(".")
    y, mo, d = map(int, date_str.split("-"))
    return datetime.datetime(y, mo, d, int(h), int(m), int(sec), int(ms) * 1000)


def _map_tool_name(name):
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
        
    parts = clean.replace("-", "_").split("_")
    return "".join(p.capitalize() for p in parts if p)


def _summarize_tool_input(name, inp):
    if not isinstance(inp, dict) or not inp:
        return ""
    
    mapped_name = _map_tool_name(name)
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

    bits = []
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


def _summarize_event(name, ev):
    if name in ("BeforeModel", "AfterModel", "BeforeToolSelection", "PreCompress"):
        return None
    if name == "MessageDisplay":
        if not ev.get("final"):
            return None
        v = ev.get("delta") or ""
        return v if len(v) <= 100 else v[:97] + "…"
    for k in ("prompt", "message", "source", "subagent_type", "agent_type"):
        v = ev.get(k)
        if isinstance(v, str) and v:
            return v if len(v) <= 100 else v[:97] + "…"
    return ""


def parse_openclaw_session(file_path, target_date_str):
    turns = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
        
    session_id = os.path.basename(file_path).replace(".jsonl", "")
    current_turn = None
    
    for line in lines:
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
            
        etype = ev.get("type")
        if etype != "message":
            continue
            
        ts_str = ev.get("timestamp") or ""
        if not ts_str:
            continue
            
        try:
            dt = parse_iso_datetime(ts_str)
        except Exception:
            continue
            
        if dt.strftime("%Y-%m-%d") != target_date_str:
            continue
            
        msg = ev.get("message") or {}
        role = msg.get("role")
        
        if role == "user":
            if current_turn:
                turns.append(current_turn)
            
            content = msg.get("content") or ""
            current_turn = {
                "start": dt,
                "prompt": content.strip() if isinstance(content, str) else str(content),
                "tools": [],
                "extras": [],
                "stop_msg": "",
                "stop_ts": None,
                "agent_name": "OpenClaw",
                "agent_display": "OpenClaw CLI",
                "sid": session_id
            }
        elif role == "assistant":
            if not current_turn:
                current_turn = {
                    "start": dt,
                    "prompt": "",
                    "tools": [],
                    "extras": [],
                    "stop_msg": "",
                    "stop_ts": None,
                    "agent_name": "OpenClaw",
                    "agent_display": "OpenClaw CLI",
                    "sid": session_id
                }
            content_list = msg.get("content") or []
            if isinstance(content_list, str):
                current_turn["stop_msg"] = content_list.strip()
            elif isinstance(content_list, list):
                text_parts = []
                for b in content_list:
                    if isinstance(b, dict) and b.get("type") == "text":
                        text_parts.append(b.get("text", ""))
                current_turn["stop_msg"] = "\n".join(text_parts).strip()
            current_turn["stop_ts"] = dt

    if current_turn:
        turns.append(current_turn)
    return turns


def parse_codex_session(file_path, target_date_str):
    turns = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
        
    session_id = os.path.basename(file_path).replace(".jsonl", "").replace("rollout-", "")
    current_turn = None
    
    for line in lines:
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
            
        ts_str = ev.get("timestamp") or ""
        if not ts_str:
            continue
            
        try:
            dt = parse_iso_datetime(ts_str)
        except Exception:
            continue
            
        if dt.strftime("%Y-%m-%d") != target_date_str:
            continue
            
        etype = ev.get("type")
        payload = ev.get("payload") or {}
        
        if etype == "event_msg":
            subtype = payload.get("type")
            if subtype == "user_message":
                if current_turn:
                    turns.append(current_turn)
                current_turn = {
                    "start": dt,
                    "prompt": payload.get("message", "").strip(),
                    "tools": [],
                    "extras": [],
                    "stop_msg": "",
                    "stop_ts": None,
                    "agent_name": "Codex",
                    "agent_display": "Codex CLI",
                    "sid": session_id
                }
            elif subtype == "agent_message":
                if not current_turn:
                    current_turn = {
                        "start": dt,
                        "prompt": "",
                        "tools": [],
                        "extras": [],
                        "stop_msg": "",
                        "stop_ts": None,
                        "agent_name": "Codex",
                        "agent_display": "Codex CLI",
                        "sid": session_id
                    }
                msg = payload.get("message", "").strip()
                if msg:
                    current_turn["stop_msg"] = (current_turn["stop_msg"] + "\n" + msg).strip()
                current_turn["stop_ts"] = dt
                
        elif etype == "response_item":
            item_payload = ev.get("payload") or {}
            role = item_payload.get("role")
            if role == "assistant":
                if not current_turn:
                    current_turn = {
                        "start": dt,
                        "prompt": "",
                        "tools": [],
                        "extras": [],
                        "stop_msg": "",
                        "stop_ts": None,
                        "agent_name": "Codex",
                        "agent_display": "Codex CLI",
                        "sid": session_id
                    }
                content = item_payload.get("content") or []
                text_parts = []
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            text_parts.append(b.get("text", ""))
                current_turn["stop_msg"] = "\n".join(text_parts).strip()
                current_turn["stop_ts"] = dt
                
    if current_turn:
        turns.append(current_turn)
    return turns


def rebuild(target_date_str):
    print(f"Rebuilding complete daily log for {target_date_str}...")
    
    # 1. Parse events_<date>.jsonl (for Claude, Gemini CLI, OpenCode)
    events_file = os.path.expanduser(f"~/data/logs/ai/events_{target_date_str}.jsonl")
    sessions_events = collections.defaultdict(list)
    if os.path.exists(events_file):
        print(f"Reading events from {events_file}...")
        with open(events_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                    sid = ev.get("session_id", "_")
                    sessions_events[sid].append(ev)
                except Exception:
                    pass
                    
    all_sessions_markdown = []
    
    for sid, sess_evs in sessions_events.items():
        sess_state = {}
        sess_turns = []
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
            
            agent_name = agent_info["name"]
            out.append(f"### {claude_ts.strftime('%H:%M:%S')}  {agent_name}")
            
            items = [(t.get("pre") or start, "tool", t) for t in sess.get("tools", [])]
            items += [(e.get("ts") or start, "extra", e) for e in sess.get("extras", [])]
            items.sort(key=lambda x: x[0])
            for _, kind, it in items:
                if kind == "tool":
                    tname = _map_tool_name(it['name'])
                    head = f"  ⚙ {tname}"
                    s = _summarize_tool_input(it['name'], it.get("input"))
                    if s:
                        head += f"   {s}"
                    pre, post = it.get("pre"), it.get("post")
                    if pre and post:
                        head += f"   +{(post - pre).total_seconds() * 1000:.0f}ms"
                    out.append(head)
                    
                    # No tool response output is printed to keep logs output-free
                    
                    if post:
                        out.append(f"  {'✘' if it.get('err') else '✔'} {tname}")
                    else:
                        out.append(f"  ⊘ {tname}   (denied / not run)")
                else:
                    out.append(f"  {it['glyph']} {it['name']}" + (f"   {it['text']}" if it.get("text") else "").rstrip())
            if sess.get("stop_msg"):
                out.append("")
                out.append(sess.get("stop_msg"))
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
                dt = parse_iso_datetime(ts_str) if "T" in ts_str or "-" in ts_str else parse_time_with_date(ts_str, target_date_str)
            except Exception:
                dt = datetime.datetime.now()
                
            if name == "UserPromptSubmit":
                if sid in sess_state:
                    flush_sess_turn()
                sess_state[sid] = {
                    "prompt": ev.get("prompt", ""),
                    "start": dt,
                    "tools": [],
                    "extras": [],
                }
            elif name == "PreToolUse":
                s = sess_state.setdefault(sid, {"prompt": "", "start": dt, "tools": [], "extras": []})
                s.setdefault("first_tool_ts", dt)
                s["tools"].append({
                    "name": ev.get("tool_name", "?"),
                    "input": ev.get("tool_input"),
                    "pre": dt,
                    "id": ev.get("tool_use_id"),
                })
            elif name == "PostToolUse":
                s = sess_state.get(sid)
                if s:
                    tool_use_id = ev.get("tool_use_id")
                    tool_name = ev.get("tool_name", "?")
                    matched = False
                    if tool_use_id:
                        for t in s["tools"]:
                            if t.get("id") == tool_use_id and not t.get("post"):
                                t["post"] = dt
                                t["err"] = False
                                matched = True
                                break
                    if not matched:
                        for t in s["tools"]:
                            if t["name"] == tool_name and not t.get("post"):
                                t["post"] = dt
                                t["err"] = False
                                break
            elif name == "PostToolUseFailure":
                s = sess_state.get(sid)
                if s:
                    tool_use_id = ev.get("tool_use_id")
                    tool_name = ev.get("tool_name", "?")
                    matched = False
                    if tool_use_id:
                        for t in s["tools"]:
                            if t.get("id") == tool_use_id and not t.get("post"):
                                t["post"] = dt
                                t["err"] = True
                                matched = True
                                break
                    if not matched:
                        for t in s["tools"]:
                            if t["name"] == tool_name and not t.get("post"):
                                t["post"] = dt
                                t["err"] = True
                                break
            elif name == "Stop":
                s = sess_state.get(sid)
                if s:
                    s["stop_ts"] = dt
                    msg = ev.get("last_assistant_message") or ev.get("prompt_response") or ev.get("message") or ev.get("response") or ""
                    if msg or not s.get("stop_msg"):
                        s["stop_msg"] = msg
                    s["stop_reason"] = ev.get("stop_reason", "")
                if s and (s.get("stop_msg") or not s.get("tools")):
                    flush_sess_turn()
            elif name == "SessionEnd":
                line = f"### {dt.strftime('%H:%M:%S')}  ◦ ⏹ SessionEnd"
                text = _summarize_event("SessionEnd", ev)
                if text:
                    line += f"   {text}"
                sess_turns.append((dt, line + "\n"))
                sess_state.pop(sid, None)
            else:
                text = _summarize_event(name, ev)
                if text is not None:
                    s = sess_state.get(sid)
                    if s is not None:
                        s["extras"].append({
                            "ts": dt,
                            "glyph": _GLYPH.get(name, "•"),
                            "name": name,
                            "text": text,
                        })
                    else:
                        line = f"### {dt.strftime('%H:%M:%S')}  ◦ {_GLYPH.get(name, '•')} {name}"
                        if text:
                            line += f"   {text}"
                        sess_turns.append((dt, line + "\n"))
                        
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

    # 2. Parse OpenClaw sessions from .openclaw/agents/main/sessions/
    openclaw_dir = os.path.expanduser("~/.openclaw/agents/main/sessions")
    if os.path.exists(openclaw_dir):
        for f in os.listdir(openclaw_dir):
            if f.endswith(".jsonl") and not f.endswith("trajectory.jsonl"):
                fp = os.path.join(openclaw_dir, f)
                try:
                    claw_turns = parse_openclaw_session(fp, target_date_str)
                    if claw_turns:
                        sess_start_time = claw_turns[0]["start"]
                        sess_md = []
                        sess_md.append(f"## ─── Session Start — OpenClaw CLI ({f.replace('.jsonl', '')}) ───")
                        sess_md.append("")
                        for t in claw_turns:
                            t_out = []
                            t_out.append(f"### {t['start'].strftime('%H:%M:%S')}  ▸ You")
                            t_out.append(t["prompt"])
                            t_out.append("")
                            t_out.append(f"### {t['stop_ts'].strftime('%H:%M:%S') if t['stop_ts'] else t['start'].strftime('%H:%M:%S')}  OpenClaw")
                            if t["stop_msg"]:
                                t_out.append(t["stop_msg"])
                            t_out.append("")
                            sess_md.append("\n".join(t_out))
                        sess_md.append(f"## ─── Session End — OpenClaw CLI ───\n")
                        all_sessions_markdown.append((sess_start_time, "\n".join(sess_md)))
                except Exception as e:
                    print(f"Error parsing OpenClaw session {f}: {e}")

    # 3. Parse Codex sessions from .codex/sessions/YYYY/MM/DD/
    try:
        y, mo, d = target_date_str.split("-")
        codex_dir = os.path.join(os.path.expanduser("~/.codex/sessions"), y, mo, d)
        if os.path.exists(codex_dir):
            for f in os.listdir(codex_dir):
                if f.endswith(".jsonl"):
                    fp = os.path.join(codex_dir, f)
                    try:
                        codex_turns = parse_codex_session(fp, target_date_str)
                        if codex_turns:
                            sess_start_time = codex_turns[0]["start"]
                            sess_md = []
                            sess_md.append(f"## ─── Session Start — Codex CLI ({f.replace('.jsonl', '').replace('rollout-', '')}) ───")
                            sess_md.append("")
                            for t in codex_turns:
                                t_out = []
                                t_out.append(f"### {t['start'].strftime('%H:%M:%S')}  ▸ You")
                                t_out.append(t["prompt"])
                                t_out.append("")
                                t_out.append(f"### {t['stop_ts'].strftime('%H:%M:%S') if t['stop_ts'] else t['start'].strftime('%H:%M:%S')}  Codex")
                                if t["stop_msg"]:
                                    t_out.append(t["stop_msg"])
                                t_out.append("")
                                sess_md.append("\n".join(t_out))
                            sess_md.append(f"## ─── Session End — Codex CLI ───\n")
                            all_sessions_markdown.append((sess_start_time, "\n".join(sess_md)))
                    except Exception as e:
                        print(f"Error parsing Codex session {f}: {e}")
    except Exception as e:
        print(f"Error scanning Codex sessions: {e}")

    # 4. Sort all sessions globally by start time and write output!
    all_sessions_markdown.sort(key=lambda x: x[0])
    
    md_file = os.path.expanduser(f"~/data/logs/ai/{target_date_str}.md")
    if os.path.exists(md_file):
        os.remove(md_file)
        
    with open(md_file, "w", encoding="utf-8") as f:
        f.write(f"# AI log — {target_date_str}\n\n")
        for _, sess_md_text in all_sessions_markdown:
            f.write(sess_md_text + "\n")
            
    print(f"Successfully rebuilt log for {target_date_str}! Daily .md file fully restored.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        try:
            target = datetime.date.fromisoformat(sys.argv[1])
        except ValueError:
            print(f"Bad date: {sys.argv[1]}  (use YYYY-MM-DD)")
            sys.exit(1)
    else:
        target = datetime.date.today()

    rebuild(target.isoformat())
