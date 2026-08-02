"""Scan locally persisted CLI state files for quota/usage data.

Providers' own CLIs (Codex, Claude Code, ...) persist rate-limit snapshots to
disk as a side effect of normal use. Reading those files costs no quota, no
network, no OAuth: it only surfaces what the machine already knows. This module
is pure (no Sublime imports) so it is unit-testable; ai_terminal runs it from a
background thread and feeds the results into the menu caption state.
"""

import json
import os
import re
import time


# ─── provider detection ──────────────────────────────────────────────────────

_PROVIDER_EXECUTABLES = {
    "codex": "codex",
    "claude": "claude",
    "gemini": "gemini",
    "qwen": "qwen",
    "kimi": "kimi",
    "grok": "grok",
    "vibe": "vibe",
    "opencode": "opencode",
    "jcode": "jcode",
    "openclaw": "openclaw",
    "mimo": "mimo",
    # Wrapped launches ("ollama launch codex") bill Ollama, not the wrapped
    # CLI's account, so the wrapper must win provider detection.
    "ollama": "ollama",
}


def provider_for_profile(profile):
    """Best-effort provider id ("codex", "claude", ...) for one profile.

    Scans argv in order and returns the first recognized executable, so a
    wrapper like "ollama launch codex" maps to the wrapper (whose account is
    the one actually billed). Returns None for plain shells / unknown tools.
    """
    if not isinstance(profile, dict):
        return None
    argv = profile.get("launch_command")
    if not isinstance(argv, (list, tuple)):
        return None
    for item in argv:
        if not isinstance(item, str):
            continue
        base = os.path.basename(item).lower()
        base = re.sub(r"\.(exe|cmd|bat|ps1)$", "", base)
        provider = _PROVIDER_EXECUTABLES.get(base)
        if provider:
            return provider
    return None


# ─── humanization ────────────────────────────────────────────────────────────

def humanize_epoch(epoch, now=None):
    """Compact human description of a future epoch: "in 3d 4h" / "in 25m"."""
    try:
        delta = int(epoch) - int(now if now is not None else time.time())
    except (TypeError, ValueError):
        return None
    if delta <= 0:
        return "now"
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return "in %dd %dh" % (days, hours)
    if hours:
        return "in %dh %dm" % (hours, minutes)
    return "in %dm" % max(minutes, 1)


# ─── codex: rate_limits snapshots in session rollout files ───────────────────

def parse_codex_rate_limits(line):
    """Extract (remaining_percent, resets_at_epoch) from one rollout line.

    Codex writes token_count events whose payload carries
    rate_limits.primary.used_percent and .resets_at. Returns None when the
    line has no usable snapshot.
    """
    if '"rate_limits"' not in line:
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    payload = event.get("payload") or {}
    limits = payload.get("rate_limits") or payload.get("info", {}).get("rate_limits")
    if not isinstance(limits, dict):
        return None
    primary = limits.get("primary")
    if not isinstance(primary, dict):
        return None
    used = primary.get("used_percent")
    if not isinstance(used, (int, float)):
        return None
    remaining = max(0.0, min(100.0, 100.0 - float(used)))
    resets_at = primary.get("resets_at")
    return remaining, resets_at if isinstance(resets_at, (int, float)) else None


def _tail_lines(path, max_bytes=131072):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()


def scan_codex_usage(codex_home, now=None, max_files=4):
    """Newest persisted Codex quota snapshot, or None.

    Walks the most recently modified rollout files under sessions/ and returns
    {"remaining": float, "reset": str|None, "observed_at": mtime}.
    """
    sessions = os.path.join(os.path.expanduser(codex_home), "sessions")
    candidates = []
    try:
        for root, _dirs, files in os.walk(sessions):
            for name in files:
                if name.endswith(".jsonl"):
                    full = os.path.join(root, name)
                    try:
                        candidates.append((os.path.getmtime(full), full))
                    except OSError:
                        pass
    except OSError:
        return None
    for mtime, path in sorted(candidates, reverse=True)[:max_files]:
        for line in reversed(_tail_lines(path)):
            parsed = parse_codex_rate_limits(line)
            if parsed is not None:
                remaining, resets_at = parsed
                return {
                    "remaining": remaining,
                    "reset": humanize_epoch(resets_at, now=now),
                    "observed_at": mtime,
                }
    return None


# ─── aggregate scan ──────────────────────────────────────────────────────────

def scan_local_usage(home=None, now=None):
    """Provider → usage dict for every provider with locally persisted data.

    Currently Codex is the only CLI known to persist machine-readable quota
    snapshots. Other providers appear here as they grow local state worth
    reading; until then their menu entries stay caption-free rather than
    claiming knowledge nothing established.
    """
    home = os.path.expanduser(home or "~")
    results = {}
    codex = scan_codex_usage(os.path.join(home, ".codex"), now=now)
    if codex:
        results["codex"] = codex
    return results
