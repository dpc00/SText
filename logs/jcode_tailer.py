"""jcode_tailer.py -- feed jcode sessions into the daily markdown log.

jcode (unlike Claude Code) has NO native HTTP hooks. Its only hook mechanism
is a `command` hook that spawns a fresh process on every single tool call.
That is wasteful (hundreds of ~1s powershell.exe spawns per session) and, on
this machine, failed outright (exit -196608). Rather than paper over that with
a faster spawned script, we delete the per-event hook entirely and read jcode's
own source of truth instead.

jcode continuously appends every turn to an append-only journal:

    ~/.jcode/sessions/<session>.journal.jsonl

Each non-meta line looks like:

    {"meta": {...}, "append_messages": [ {id, role, content, timestamp}, ... ]}

where `content` is a list of blocks: text / reasoning_trace / tool_use /
tool_result. We tail these journals from a single long-lived thread and
translate each message into the same hook-event shape the log server already
understands (UserPromptSubmit / PreToolUse / PostToolUse / Stop), then hand it
to ai_log_server.process_event(). One thread, no per-event processes.

State (byte offset + seen message ids per journal) is persisted so a server
restart resumes without re-emitting old turns.
"""
from __future__ import annotations

import json
import os
import threading
import time

SESS_DIR = os.path.join(os.path.expanduser("~"), ".jcode", "sessions")
STATE_DIR = os.path.join(os.path.expanduser("~"), "data", "logs", ".jcode_tail")
POLL_SECS = 1.0
# Ignore journals that have not been touched in this many seconds on first
# scan, so a server (re)start does not replay weeks of history. Live sessions
# update every few seconds, so this only skips the long-dormant ones.
STALE_SECS = 6 * 3600

_AGENT = "Jcode"


def _load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"offset": 0, "seen": []}


def _save_state(path: str, state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _tool_name(raw: str) -> str:
    return raw or "?"


def _content_blocks(msg: dict):
    c = msg.get("content")
    if isinstance(c, list):
        return c
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return []


def _text_of(blocks) -> str:
    parts = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str) and t.strip():
                parts.append(t)
    return "\n".join(parts)


def _emit(process_event, ev):
    try:
        process_event(ev)
    except Exception:
        pass


def _handle_message(process_event, sid, cwd, msg):
    """Translate one journal message into hook events fed to the log server.

    jcode's turn model mirrors Claude's: a single user prompt drives many
    assistant messages (each reasoning + tool_use), interleaved with user
    messages that carry the tool_result blocks, until the assistant finally
    answers with a text-only message. We therefore:

      * open a turn on a user *text* prompt (UserPromptSubmit),
      * pair tool_result blocks from user messages as PostToolUse,
      * register tool_use blocks from assistant messages as PreToolUse,
      * keep the assistant's latest text as the closing message but DEFER the
        flush while the model is still calling tools, and force the flush only
        when the assistant answers with text and no further tool calls.

    This yields one clean "Jcode" block per user prompt, with correct tool
    pairing (no spurious "denied") and no empty prompt blocks.
    """
    role = msg.get("role")
    blocks = _content_blocks(msg)

    if role == "user":
        # Real prompts carry text; tool feedback carries tool_result blocks.
        # These are effectively never mixed in the same message.
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        for r in results:
            is_err = _looks_like_error(r.get("content"))
            _emit(process_event, {
                "hook_event_name": "PostToolUseFailure" if is_err else "PostToolUse",
                "agent": _AGENT,
                "session_id": sid,
                "cwd": cwd,
                "tool_use_id": r.get("tool_use_id"),
                "tool_response": r.get("content"),
            })
        prompt = _text_of(blocks)
        if prompt.strip():
            _emit(process_event, {
                "hook_event_name": "UserPromptSubmit",
                "agent": _AGENT,
                "session_id": sid,
                "cwd": cwd,
                "prompt": prompt,
            })
        return

    if role == "assistant":
        tool_calls = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        for tc in tool_calls:
            _emit(process_event, {
                "hook_event_name": "PreToolUse",
                "agent": _AGENT,
                "session_id": sid,
                "cwd": cwd,
                "tool_name": _tool_name(tc.get("name")),
                "tool_input": tc.get("input"),
                "tool_use_id": tc.get("id"),
            })
        text = _text_of(blocks)
        if text.strip():
            # Text alongside tool calls is mid-turn narration: record it as the
            # (current) closing message but keep the turn open. Text with no
            # tool calls is the final answer: close the turn now.
            final = not tool_calls
            _emit(process_event, {
                "hook_event_name": "Stop",
                "agent": _AGENT,
                "session_id": sid,
                "cwd": cwd,
                "last_assistant_message": text,
                "defer_flush": not final,
                "force_flush": final,
            })
        return


def _looks_like_error(content) -> bool:
    if not isinstance(content, str):
        return False
    head = content[:80].lower()
    return head.startswith("error:") or "\nexit code: 1" in content.lower()[:200]


def _process_journal(process_event, journal_path, state_path):
    state = _load_state(state_path)
    offset = int(state.get("offset", 0))
    seen = set(state.get("seen", []))

    try:
        size = os.path.getsize(journal_path)
    except OSError:
        return
    if size < offset:
        # File truncated/rotated; restart from the top.
        offset = 0
        seen = set()

    if offset >= size:
        return

    try:
        with open(journal_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            data = f.read()
            new_offset = f.tell()
    except OSError:
        return

    sid = os.path.basename(journal_path).split(".journal")[0]
    cwd = ""
    changed = False
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        meta = obj.get("meta") or {}
        wd = meta.get("working_dir")
        if isinstance(wd, str) and wd:
            cwd = wd
        for msg in obj.get("append_messages") or []:
            mid = msg.get("id")
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            _handle_message(process_event, sid, cwd, msg)
            changed = True

    if new_offset != offset or changed:
        # Bound the seen set so it cannot grow without limit.
        seen_list = list(seen)
        if len(seen_list) > 5000:
            seen_list = seen_list[-5000:]
        _save_state(state_path, {"offset": new_offset, "seen": seen_list})


def _loop(process_event):
    os.makedirs(STATE_DIR, exist_ok=True)
    first_scan = True
    while True:
        try:
            names = [n for n in os.listdir(SESS_DIR) if n.endswith(".journal.jsonl")]
        except OSError:
            names = []
        now = time.time()
        for name in names:
            journal_path = os.path.join(SESS_DIR, name)
            state_path = os.path.join(STATE_DIR, name + ".state")
            if first_scan and not os.path.exists(state_path):
                # On the very first scan after a (re)start, skip long-dormant
                # journals so we do not replay old history, but still record
                # where they ended so future appends are captured.
                try:
                    if now - os.path.getmtime(journal_path) > STALE_SECS:
                        try:
                            _save_state(state_path, {
                                "offset": os.path.getsize(journal_path),
                                "seen": [],
                            })
                        except OSError:
                            pass
                        continue
                except OSError:
                    pass
            try:
                _process_journal(process_event, journal_path, state_path)
            except Exception:
                pass
        first_scan = False
        time.sleep(POLL_SECS)


_started = False


def start(process_event):
    """Launch the tailer thread once. `process_event` is the log server sink."""
    global _started
    if _started:
        return
    _started = True
    t = threading.Thread(target=_loop, args=(process_event,), daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    # Standalone smoke: print events instead of logging them.
    def _printer(ev):
        print(json.dumps(ev)[:200])
    _loop(_printer)
