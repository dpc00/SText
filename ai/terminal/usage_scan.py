"""Amass quota/usage data for AI CLI profiles from the source, once.

Two tiers, both free of inference quota:

1. **Live usage endpoints** — providers expose dedicated usage APIs that their
   own CLIs call (Codex: chatgpt.com/backend-api/wham/usage; Claude:
   api.anthropic.com/api/oauth/usage). We call them with the OAuth tokens the
   CLIs already persisted to disk. This is the accurate, from-the-source data:
   every rate-limit window (5h, weekly, ...) with exact reset times.
2. **Local state fallback** — when a live call fails (offline, token expired),
   the newest persisted snapshot in the CLI's own session files is used.

This module is pure Python (no Sublime imports) so it is unit-testable;
ai_terminal runs ``gather_usage`` from a one-shot background thread at plugin
load. The full sweep can take minutes; nothing blocks the UI.
"""

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request


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

    Fallback tier only: prefer ``gather_usage`` which asks the providers'
    own usage endpoints. Codex is currently the only CLI persisting
    machine-readable quota snapshots locally.
    """
    home = os.path.expanduser(home or "~")
    results = {}
    codex = scan_codex_usage(os.path.join(home, ".codex"), now=now)
    if codex:
        results["codex"] = codex
    return results


# ─── live usage endpoints (the source of truth) ──────────────────────────────

def _http_json(url, headers, timeout=20):
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _iso_to_epoch(value):
    """Epoch seconds for an ISO-8601 string (or passthrough numeric)."""
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return None
    try:
        import datetime

        return datetime.datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return None


def window_label(seconds):
    """Human window name from its length: 18000→"5h", 604800→"weekly"."""
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    hours = seconds / 3600.0
    if hours <= 24:
        return "%gh" % round(hours)
    days = seconds / 86400.0
    return "weekly" if 6.5 <= days <= 7.5 else "%gd" % round(days)


def summarize_windows(windows, now=None):
    """One compact line for all rate-limit windows.

    e.g. "5h 100% left · weekly 47% left (resets in 6d 3h)". The reset of
    the most constrained (lowest-remaining) window is shown.
    """
    parts = []
    tightest_with_reset = None
    for win in windows:
        label = win.get("label") or "?"
        remaining = win.get("remaining")
        if not isinstance(remaining, (int, float)):
            continue
        parts.append("%s %g%% left" % (label, remaining))
        if win.get("reset") and (
            tightest_with_reset is None
            or remaining < tightest_with_reset.get("remaining", 101)
        ):
            tightest_with_reset = win
    if not parts:
        return None
    summary = " · ".join(parts)
    if tightest_with_reset:
        summary += " (resets %s)" % tightest_with_reset["reset"]
    return summary


def parse_codex_wham_usage(payload, now=None):
    """Usage dict from chatgpt.com/backend-api/wham/usage JSON.

    ``primary_window`` is the weekly window; ``secondary_window`` is the 5h
    window and is null when that window currently has no usage (100% left).
    """
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None
    windows = []
    secondary = rate_limit.get("secondary_window")
    if isinstance(secondary, dict):
        windows.append({
            "label": window_label(secondary.get("limit_window_seconds")) or "5h",
            "remaining": max(0.0, 100.0 - float(secondary.get("used_percent") or 0)),
            "reset": humanize_epoch(secondary.get("reset_at"), now=now),
        })
    else:
        # Codex reports the 5h window as null while it is empty.
        windows.append({"label": "5h", "remaining": 100.0, "reset": None})
    primary = rate_limit.get("primary_window")
    if isinstance(primary, dict):
        windows.append({
            "label": window_label(primary.get("limit_window_seconds")) or "weekly",
            "remaining": max(0.0, 100.0 - float(primary.get("used_percent") or 0)),
            "reset": humanize_epoch(primary.get("reset_at"), now=now),
        })
    remaining_values = [
        w["remaining"] for w in windows if isinstance(w.get("remaining"), (int, float))
    ]
    if not remaining_values:
        return None
    return {
        "windows": windows,
        "remaining": min(remaining_values),
        "summary": summarize_windows(windows, now=now),
        "source": "live",
        "observed_at": now if now is not None else time.time(),
    }


def fetch_codex_usage(codex_home="~/.codex", now=None):
    """Live Codex quota from the usage endpoint its own CLI uses.

    Reuses the OAuth access token Codex persisted in auth.json; a dedicated
    usage endpoint, so no inference quota is spent. Returns None on any
    failure (offline, logged out, token expired).
    """
    auth_path = os.path.join(os.path.expanduser(codex_home), "auth.json")
    try:
        with open(auth_path, "r", encoding="utf-8") as handle:
            auth = json.load(handle)
    except (OSError, ValueError):
        return None
    tokens = auth.get("tokens") or {}
    access_token = tokens.get("access_token")
    account_id = tokens.get("account_id")
    if not access_token:
        return None
    headers = {
        "Authorization": "Bearer %s" % access_token,
        "OpenAI-Beta": "codex-1",
        "Originator": "Codex CLI",
        "Accept": "application/json",
    }
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    try:
        payload = _http_json("https://chatgpt.com/backend-api/wham/usage", headers)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return parse_codex_wham_usage(payload, now=now)


_CLAUDE_WINDOW_LABELS = {
    "five_hour": "5h",
    "seven_day": "weekly",
    "seven_day_opus": "weekly Opus",
    "seven_day_sonnet": "weekly Sonnet",
}


def parse_claude_oauth_usage(payload, now=None):
    """Usage dict from api.anthropic.com/api/oauth/usage JSON.

    Windows arrive as keys like five_hour/seven_day carrying ``utilization``
    (percent used) and ``resets_at`` (ISO timestamp). Unknown keys pass
    through with their raw name so new windows still surface.
    """
    windows = []
    for key, value in payload.items():
        if not isinstance(value, dict) or "utilization" not in value:
            continue
        used = value.get("utilization")
        if not isinstance(used, (int, float)):
            continue
        windows.append({
            "label": _CLAUDE_WINDOW_LABELS.get(key, key.replace("_", " ")),
            "remaining": round(max(0.0, min(100.0, 100.0 - float(used))), 1),
            "reset": humanize_epoch(_iso_to_epoch(value.get("resets_at")), now=now),
        })
    if not windows:
        return None
    return {
        "windows": windows,
        "remaining": min(w["remaining"] for w in windows),
        "summary": summarize_windows(windows, now=now),
        "source": "live",
        "observed_at": now if now is not None else time.time(),
    }


def fetch_claude_usage(claude_home="~/.claude", now=None):
    """Live Claude quota from Anthropic's OAuth usage endpoint.

    Uses the access token Claude Code persisted in .credentials.json. When it
    has expired, performs the same refresh-token grant the CLI itself uses
    and atomically writes the rotated tokens back, so the CLI stays logged
    in (verified: ``claude auth status`` still reports loggedIn afterwards).
    """
    claude_home = os.path.expanduser(claude_home)
    creds_path = os.path.join(claude_home, ".credentials.json")
    oauth = _read_claude_oauth(creds_path)
    if not oauth or not oauth.get("accessToken"):
        return None
    if _claude_token_expired(oauth, now=now):
        oauth = _refresh_claude_token(creds_path, oauth)
        if not oauth:
            return {
                "error": "token expired — open Claude to refresh",
                "source": "live",
            }
    headers = {
        "Authorization": "Bearer %s" % oauth["accessToken"],
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
    }
    try:
        payload = _http_json("https://api.anthropic.com/api/oauth/usage", headers)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return parse_claude_oauth_usage(payload, now=now)


def _read_claude_oauth(creds_path):
    try:
        with open(creds_path, "r", encoding="utf-8") as handle:
            creds = json.load(handle)
    except (OSError, ValueError):
        return None
    oauth = creds.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else None


def _claude_token_expired(oauth, now=None, margin_seconds=60):
    expires_at = oauth.get("expiresAt")  # epoch milliseconds
    if not isinstance(expires_at, (int, float)):
        return False  # no expiry recorded — let the endpoint be the judge
    now_ms = (now if now is not None else time.time()) * 1000.0
    return expires_at <= now_ms + margin_seconds * 1000.0


# Client id Claude Code itself uses for the OAuth device flow (public, it is
# embedded in every install of the CLI).
_CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"


def _refresh_claude_token(creds_path, oauth):
    """Refresh the Claude access token exactly like the CLI does, or None.

    The refresh grant rotates the refresh token, so the rotated pair is
    atomically persisted back to .credentials.json before returning —
    otherwise the CLI's next refresh would fail and log the user out.
    If the grant is rejected (e.g. a concurrently running CLI just rotated
    the token first), the file is re-read once: the winner's fresh token
    serves fine.
    """
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        return None
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _CLAUDE_OAUTH_CLIENT_ID,
    }).encode("utf-8")
    request = urllib.request.Request(
        _CLAUDE_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "claude-cli/2.0 (external, cli)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        latest = _read_claude_oauth(creds_path)
        if latest and not _claude_token_expired(latest):
            return latest  # someone else (the CLI) refreshed first — use theirs
        return None
    access_token = payload.get("access_token")
    if not access_token:
        return None
    oauth = dict(oauth)
    oauth["accessToken"] = access_token
    if payload.get("refresh_token"):
        oauth["refreshToken"] = payload["refresh_token"]
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)):
        oauth["expiresAt"] = int((time.time() + expires_in) * 1000)
    scope = payload.get("scope")
    if isinstance(scope, str) and scope:
        oauth["scopes"] = scope.split(" ")
    _persist_claude_oauth(creds_path, oauth)
    return oauth


def _persist_claude_oauth(creds_path, oauth):
    """Atomically merge the rotated oauth block back into .credentials.json."""
    try:
        with open(creds_path, "r", encoding="utf-8") as handle:
            creds = json.load(handle)
    except (OSError, ValueError):
        creds = {}
    creds["claudeAiOauth"] = oauth
    directory = os.path.dirname(creds_path) or "."
    try:
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(creds, handle)
        os.replace(tmp_path, creds_path)
    except OSError:
        pass  # keeping the in-memory token still lets this sweep finish


def parse_ollama_me(payload):
    """Usage dict from the local ollama server's /api/me (account info).

    Ollama exposes no quota/usage endpoint (verified against the binary's
    string table), so the honest best is the signed-in account and plan.
    """
    if not isinstance(payload, dict):
        return None
    plan = payload.get("plan")
    if not isinstance(plan, str) or not plan:
        return None
    name = payload.get("name") or payload.get("email")
    summary = "cloud plan: %s" % plan
    if isinstance(name, str) and name:
        summary += " (%s)" % name
    summary += " — usage not exposed"
    return {"summary": summary, "plan": plan, "source": "live"}


def fetch_ollama_usage(base_url="http://localhost:11434", now=None):
    """Account/plan from the local ollama server, or None when not running.

    POST /api/me makes the local server call home with its own key auth;
    no inference quota is spent.
    """
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/me",
        data=b"{}",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return parse_ollama_me(payload)


def parse_openrouter_key(payload):
    """Usage dict from openrouter.ai/api/v1/key JSON.

    Dollar spend is exact; the free tier's daily request cap (50/day for
    :free models) is not exposed by the API, so the tier is named instead of
    inventing a percentage.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    parts = []
    if data.get("is_free_tier"):
        parts.append("free tier")
    limit = data.get("limit")
    remaining = data.get("limit_remaining")
    if isinstance(limit, (int, float)) and isinstance(remaining, (int, float)):
        parts.append("$%.2f of $%.2f credit left" % (remaining, limit))
    usage_daily = data.get("usage_daily")
    if isinstance(usage_daily, (int, float)):
        parts.append("$%.2f today" % usage_daily)
    usage = data.get("usage")
    if isinstance(usage, (int, float)):
        parts.append("$%.2f total" % usage)
    if not parts:
        return None
    result = {"summary": "OpenRouter: " + " · ".join(parts), "source": "live"}
    if isinstance(remaining, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
        result["remaining"] = round(100.0 * remaining / limit, 1)
    return result


def _openrouter_key_from_qwen(qwen_home):
    """The OpenRouter API key qwen persisted in its settings, or None."""
    settings_path = os.path.join(os.path.expanduser(qwen_home), "settings.json")
    try:
        with open(settings_path, "r", encoding="utf-8") as handle:
            settings = json.load(handle)
    except (OSError, ValueError):
        return None
    env = settings.get("env")
    if isinstance(env, dict):
        key = env.get("OPENROUTER_API_KEY")
        if isinstance(key, str) and key.startswith("sk-or-"):
            return key
    return None


def fetch_openrouter_usage(qwen_home="~/.qwen", now=None):
    """Live OpenRouter key status (qwen bills through OpenRouter).

    GET /api/v1/key is a metadata endpoint — free, no inference quota.
    """
    key = _openrouter_key_from_qwen(qwen_home) or os.environ.get(
        "OPENROUTER_API_KEY"
    )
    if not key:
        return None
    headers = {"Authorization": "Bearer %s" % key, "Accept": "application/json"}
    try:
        payload = _http_json("https://openrouter.ai/api/v1/key", headers)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return parse_openrouter_key(payload)


def gather_usage(home=None, now=None):
    """Provider → usage dict: live endpoints first, local files as fallback.

    This is the slow, thorough sweep — run it once from a background thread
    at plugin load. Each provider is independent; one failure never hides
    another's data.
    """
    home_dir = os.path.expanduser(home or "~")
    results = {}

    codex = fetch_codex_usage(os.path.join(home_dir, ".codex"), now=now)
    if not codex or "windows" not in codex:
        local = scan_codex_usage(os.path.join(home_dir, ".codex"), now=now)
        if local:
            local["source"] = "local"
            codex = local
    if codex:
        results["codex"] = codex

    claude = fetch_claude_usage(os.path.join(home_dir, ".claude"), now=now)
    if claude:
        results["claude"] = claude

    ollama = fetch_ollama_usage(now=now)
    if ollama:
        results["ollama"] = ollama

    # Qwen Code is configured to bill through OpenRouter (its settings.json
    # persists the key), so the OpenRouter key status IS qwen's quota.
    openrouter = fetch_openrouter_usage(os.path.join(home_dir, ".qwen"), now=now)
    if openrouter:
        results["qwen"] = openrouter

    return results
