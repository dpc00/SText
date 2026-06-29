"""
rebuild_log.py — Rebuild ai_YYYY-MM-DD.log from JSONL source files.

Usage:
    python rebuild_log.py [date]       # date defaults to today (YYYY-MM-DD)
    python rebuild_log.py 2026-06-26

Reads every *.jsonl under ~/.claude/projects/*/
Keeps only records whose UTC timestamp falls on the target date (local time).
Sorts chronologically, deduplicates by UUID, writes a clean log.
Output goes to ~/.cache/conversation_logs/claude_DATE.log  (overwrites existing).
"""

import datetime
import json
import os
import sys
from pathlib import Path


# --------------------------------------------------------------------------- #
#  Config
# --------------------------------------------------------------------------- #

PROJECTS_DIR = Path.home() / ".claude" / "projects"
LOG_DIR = Path.home() / ".cache" / "conversation_logs"


# --------------------------------------------------------------------------- #
#  Formatting helpers  (mirrors ai_logger.py without sublime imports)
# --------------------------------------------------------------------------- #

def _fmt_ts(iso_ts):
    try:
        dt = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M:%S")
    except Exception:
        return "??:??:??"


def _ts_local_date(iso_ts):
    """Return the local date for a UTC ISO timestamp string."""
    try:
        dt = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone().date()
    except Exception:
        return None


def _flatten_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _fmt_tool(name, inp):
    if name == "Bash":
        cmd = (inp.get("command") or "").strip().replace("\n", "; ")
        return cmd[:120] + ("…" if len(cmd) > 120 else "")
    if name == "Read":
        return inp.get("file_path", "")
    if name in ("Edit", "Write"):
        return inp.get("file_path", "")
    if name == "Glob":
        pat = inp.get("pattern", "")
        path = inp.get("path", "")
        return pat + (f" in {path}" if path else "")
    if name == "Grep":
        pat = inp.get("pattern", "")
        path = inp.get("path", "")
        return pat + (f" in {path}" if path else "")
    if name == "WebSearch":
        return inp.get("query", "")
    if name == "WebFetch":
        return inp.get("url", "")
    if name == "Agent":
        desc = inp.get("description") or inp.get("prompt") or ""
        return desc[:80] + ("…" if len(desc) > 80 else "")
    if name == "Skill":
        return inp.get("skill", "")
    short = name.split("__")[-1] if "__" in name else name
    for key in ("code", "key", "pattern", "command", "text", "path", "url", "query"):
        val = inp.get(key)
        if val and isinstance(val, str):
            val = val[:60].replace("\n", "; ")
            return f"{short}: {val}"
    return short


def _record_to_lines(record, id2name):
    if record.get("isMeta") or record.get("isSidechain"):
        return []
    rtype = record.get("type")
    ts = _fmt_ts(record.get("timestamp", ""))
    out = []

    if rtype == "assistant":
        msg = record.get("message") or {}
        for b in msg.get("content", []) or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "thinking":
                continue
            if bt == "text":
                text = (b.get("text") or "").strip()
                if text:
                    out.append(f"\n[{ts}] Claude:\n{text}")
            elif bt == "tool_use":
                name = b.get("name", "?")
                id2name[b.get("id", "")] = name
                inp = b.get("input") or {}
                detail = _fmt_tool(name, inp)
                out.append(
                    f"  [{ts}] -> {name}: {detail}" if detail else f"  [{ts}] -> {name}"
                )

    elif rtype == "user":
        msg = record.get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            text = c.strip()
            if text:
                out.append(f"\n[{ts}] You: {text}")
        elif isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    tool_name = id2name.get(b.get("tool_use_id", ""), "?")
                    content = _flatten_text(b.get("content") or "")
                    preview = content[:300].replace("\n", " | ")
                    if len(content) > 300:
                        preview += f" ... (+{len(content)-300} chars)"
                    mark = "x" if b.get("is_error") else "ok"
                    out.append(f"  [{ts}] {mark} {tool_name}: {preview}")
                elif b.get("type") == "text":
                    text = (b.get("text") or "").strip()
                    if text:
                        out.append(f"\n[{ts}] You: {text}")

    elif rtype == "system":
        st = record.get("subtype")
        if st == "turn_duration":
            ms = record.get("durationMs", 0)
            out.append(f"  [{ts}] {ms/1000:.1f}s")
        elif st == "api_error":
            err = (record.get("error") or {}).get("message") or "API error"
            out.append(f"  [{ts}] API error: {err}")
        elif st == "compact_boundary":
            out.append(f"\n[{ts}] --- context compacted ---\n")
        elif st == "local_command":
            cmd = (record.get("content") or "").strip()
            if cmd:
                out.append(f"  [{ts}] [cmd] {cmd}")

    return out


# --------------------------------------------------------------------------- #
#  Main rebuild
# --------------------------------------------------------------------------- #

def rebuild(target_date: datetime.date):
    print(f"Rebuilding log for {target_date}")

    # Collect all records from all JSONL files
    all_records = []   # list of (sort_key, record, source_file)
    seen_uuids = set()
    id2name = {}

    if not PROJECTS_DIR.exists():
        print(f"ERROR: {PROJECTS_DIR} does not exist")
        sys.exit(1)

    jsonl_files = []
    for slug_dir in PROJECTS_DIR.iterdir():
        if not slug_dir.is_dir():
            continue
        for jf in slug_dir.glob("*.jsonl"):
            jsonl_files.append(jf)

    print(f"Found {len(jsonl_files)} JSONL files")

    for jf in jsonl_files:
        try:
            text = jf.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"  SKIP {jf}: {e}")
            continue

        file_records = 0
        file_kept = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            file_records += 1

            # Filter to target date (local time)
            ts_str = record.get("timestamp", "")
            rec_date = _ts_local_date(ts_str) if ts_str else None
            if rec_date != target_date:
                continue

            # Deduplicate by UUID
            uid = record.get("uuid")
            if uid:
                if uid in seen_uuids:
                    continue
                seen_uuids.add(uid)

            # Sort key: ISO timestamp string sorts lexicographically
            sort_key = ts_str

            all_records.append((sort_key, record))
            file_kept += 1

        if file_kept:
            print(f"  {jf.name}: {file_records} records, {file_kept} kept for {target_date}")

    print(f"Total records for {target_date}: {len(all_records)}")

    # Sort chronologically
    all_records.sort(key=lambda x: x[0])

    # Format
    lines_out = []
    for _, record in all_records:
        lines_out.extend(_record_to_lines(record, id2name))

    # Write log
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"claude_{target_date.isoformat()}.log"

    content = "\n".join(lines_out) + "\n"
    log_path.write_text(content, encoding="utf-8")
    print(f"Written: {log_path} ({len(content)} bytes, {len(lines_out)} lines)")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        try:
            target = datetime.date.fromisoformat(sys.argv[1])
        except ValueError:
            print(f"Bad date: {sys.argv[1]}  (use YYYY-MM-DD)")
            sys.exit(1)
    else:
        target = datetime.date.today()

    rebuild(target)
