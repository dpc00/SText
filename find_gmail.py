import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sessions = Path.home() / ".codex" / "sessions"
results = []


def extract_text(payload):
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        return " ".join(
            item.get("text", "")
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    if isinstance(payload, dict):
        if "content" in payload:
            return extract_text(payload.get("content"))
        if "message" in payload:
            return extract_text(payload.get("message"))
    return ""


for jf in sessions.rglob("*.jsonl"):
    try:
        turns = []
        pending = None
        project = str(jf.parent)
        with open(jf, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                payload = obj.get("payload") or {}
                if obj.get("type") == "session_meta":
                    project = payload.get("cwd") or project
                    continue
                if obj.get("type") != "response_item" or payload.get("type") != "message":
                    continue

                role = payload.get("role")
                text = extract_text(payload).strip()
                if role == "user" and text and not text.startswith("<"):
                    pending = {"u": text, "a": "", "ts": obj.get("timestamp", "")}
                elif role == "assistant" and pending:
                    pending["a"] = text
                    turns.append(pending)
                    pending = None

        for turn in turns:
            hay = (turn["u"] + " " + turn["a"]).lower()
            if "gmail" in hay and any(w in hay for w in ("disappear", "missing", "logout", "login", "gone", "lost", "removed")):
                results.append({"file": str(jf), "project": project, "ts": turn["ts"], "u": turn["u"][:400], "a": turn["a"][:800]})
    except Exception:
        pass

results.sort(key=lambda x: x["ts"])
for r in results:
    print("===", r["project"], "|", r["ts"][:16], "===")
    print("YOU:", r["u"])
    print("CODEX:", r["a"])
    print()
