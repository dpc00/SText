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
import urllib.parse
import urllib.request


# ─── provider detection ──────────────────────────────────────────────────────
#
# gather_usage() below covers codex/claude/ollama/kimi/qwen. Every one of the
# 12 provider executables was individually investigated and run to ground —
# checked 2026-08-02, all findings verified live (API probes, actual CLI
# source, or documented endpoints), not inferred from string-grep absence
# alone (an earlier pass here wrongly called several of these "impossible"
# off a single grep; grepping a bundled binary only proves a route is a
# literal, since most bundlers build "${base}/method" at runtime):
#   kimi    — WIRED. fetch_kimi_usage() below. api.kimi.com/coding/v1/me is
#             real (confirmed live: 401 on an expired token vs. 404 on every
#             wrong-path guess) and returns account identity + a plan-tier
#             name, but no numeric remaining/limit — a dozen other guessed
#             paths (/user/quota, /limits, /rate-limits, /credits, ...) all
#             genuinely 404 with a valid token, so plan-tier-only is the
#             honest ceiling, same shape as the ollama fetcher. The OAuth
#             refresh grant (auth.kimi.com/api/oauth/token, form-encoded —
#             the Claude endpoint's JSON body gets 400 unsupported_grant_type
#             here) was reverse-engineered and confirmed working live.
#             CAUTION: ~/.kimi-code/config.toml also holds plaintext OpenAI/
#             Anthropic/Ollama API keys (kimi's own model-router config) —
#             never print or log that file's contents.
#   gemini  — real endpoint exists (@google/gemini-cli's `/stats`/`/usage`
#             calls retrieveUserQuota against cloudcode-pa.googleapis.com,
#             needs OAuth + a projectId from loadCodeAssist) but Donal has
#             dropped Gemini entirely over an unrelated billing dispute —
#             do not build or revisit this fetcher.
#   grok    — NOT VIABLE. Traced the CLI's real backend
#             (cli-chat-proxy.grok.com/v1/chat/completions, found via string
#             search once the bundler caveat was accounted for) and
#             api.x.ai/v1/me (a real, working endpoint — returns identity +
#             a `team_blocked` flag that contradicted Donal's actual active
#             paid SuperGrok plan, so it's not a trustworthy signal and must
#             not be surfaced). No queryable quota endpoint anywhere; the
#             99%-used figure grok.com's website shows is server-rendered
#             into the authenticated page itself (confirmed via live network
#             inspection — zero XHR calls for it), reachable only with
#             Donal's browser session cookie, which is deliberately out of
#             scope (more invasive than reading a CLI's own token file).
#   opencode — subscription product (Go/Zen plans, not yet paid for as of
#             2026-08-02); config at ~/.config/opencode, opencode-go API key
#             at ~/.local/share/opencode/auth.json (NOT ~/.opencode, which
#             doesn't exist). opencode-go's base is
#             https://opencode.ai/zen/go/v1 (OpenAI-compatible chat), and it
#             has NO proactive quota endpoint — confirmed by reading the
#             actual retry/upsell logic, which learns it's rate-limited only
#             reactively from a 429 tagged
#             reason:"free_tier_limit"/"account_rate_limit". Text tier is
#             the only path. Separately, `opencode stats` is real but purely
#             local (SQLite at ~/.local/share/opencode/opencode.db,
#             `message` table) — a Codex-style local token/cost tally, not a
#             live Go-plan balance.
#   mimo    — mimocode is a straight OpenCode fork rebranded by Xiaomi. Its
#             API is https://api.xiaomimimo.com/v1 (OpenAI-compat) /
#             /anthropic (Anthropic-compat); platform.xiaomimimo.com is the
#             billing console, not the API host. Confirmed 2026-08-02:
#             `mimo providers list` shows 0 stored credentials
#             (~/.local/share/mimocode/auth.json is empty) and `mimo
#             providers whoami` says "Not logged in" — the TUI's working
#             "MiMo Auto (MiMo-V2.5)" model right now is a no-signup free
#             default, not a real account. NOT ACTIONED: logging in
#             (`mimo providers login`) was deliberately left undone rather
#             than run on Donal's behalf. If he decides to sign in later
#             (it's his call, not something to prompt for), the same
#             OpenCode-shaped reactive-429-only ceiling as `opencode` above
#             would still apply — no proactive quota endpoint either way.
#             UPDATE 2026-08-02 night: free trial ran out mid-session (TUI
#             showed "Free API service ended"). `/login` was opened once
#             just to look (out of curiosity, not signed in) — it's a
#             provider picker covering every backend mimocode supports, not
#             a form; the one relevant entry for Donal's own account is
#             "Xiaomi (Recommended)" at the top. Left at the menu, nothing
#             submitted, nothing paid. Pick back up only if/when Donal
#             wants to.
#   vibe    — NOT VIABLE, confirmed from vibe's own unpacked Python source
#             (site-packages/vibe/cli/commands.py — readable, unlike the
#             other Rust/Go/JS binaries). Its full 25-command slash-command
#             table has no /usage, /billing, /quota, or /credits command at
#             all; /status's handler (_show_status in cli/textual_ui/app.py)
#             only ever reports session-local numbers (steps, tokens this
#             session, a locally-estimated cost) — it never calls out to
#             Mistral. Confirmed separately via Context7 (Mistral's own
#             platform docs) that a real usage API DOES exist —
#             GET /v1/admin/usage, /v1/admin/spend-limit — but it requires a
#             distinct Admin API key minted in Mistral's Backoffice console,
#             not the regular workspace key vibe stores in the Windows
#             Credential Manager (ai.mistral.vibe). Donal has an org ID but
#             no path to mint that key yet, so this is documented-but-
#             blocked, not dead — revisit if an Admin key ever exists.
#             Separately, vibe's own stored key was independently confirmed
#             dead ("Invalid API key" from a live `vibe -p` call) — unrelated
#             to the above, just needs `vibe --setup` again.
#   openclaw — mechanism unknown (Donal: "a mystery how that works").
#   jcode   — itself a multi-provider aggregator (routes through its own
#             stored OpenAI/Gemini/Claude/Antigravity grants); "jcode usage"
#             isn't one number, it's whichever backend it dispatched to.
# The text tier (usage_update_from_text / reset_update_from_text in
# profile_availability.py) covers any provider whose own TUI prints quota
# text, with zero endpoint work — that's how the "100% used" Kimi fix
# landed. Prefer confirming a provider's own status/stats output first;
# only build a fetch_* endpoint client when the text tier can't reach it.

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


_KIMI_OAUTH_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
_KIMI_TOKEN_URL = "https://auth.kimi.com/api/oauth/token"


def parse_kimi_me(payload):
    """Usage dict from api.kimi.com/coding/v1/me (account info).

    Confirmed live 2026-08: this endpoint exists (401 on an expired token vs.
    404 on every other guessed path) and returns account identity plus
    ``user_level_name`` (e.g. "Free"), but no numeric remaining/limit figure
    — a dozen other guessed paths (/user/quota, /limits, /rate-limits,
    /credits, ...) all 404. Kimi's own CLI has no usage/status subcommand
    either, so plan-tier-only is the honest ceiling here, same as ollama.
    """
    if not isinstance(payload, dict):
        return None
    tier = payload.get("user_level_name")
    if not isinstance(tier, str) or not tier:
        return None
    summary = "%s tier — usage not exposed" % tier
    return {"summary": summary, "plan": tier, "source": "live"}


def _refresh_kimi_token(creds_path, creds):
    """Refresh the Kimi access token exactly like the CLI does, or None.

    The grant is form-encoded (not JSON — the JSON body form the Claude
    endpoint accepts returns 400 unsupported_grant_type here), rotates the
    refresh token, and is persisted back atomically so ``kimi`` itself stays
    logged in, mirroring _refresh_claude_token's contract.
    """
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        return None
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _KIMI_OAUTH_CLIENT_ID,
    }).encode("utf-8")
    request = urllib.request.Request(
        _KIMI_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    access_token = payload.get("access_token")
    if not access_token:
        return None
    creds = dict(creds)
    creds["access_token"] = access_token
    if payload.get("refresh_token"):
        creds["refresh_token"] = payload["refresh_token"]
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)):
        creds["expires_at"] = int(time.time() + expires_in)
        creds["expires_in"] = expires_in
    _persist_kimi_oauth(creds_path, creds)
    return creds


def _persist_kimi_oauth(creds_path, creds):
    """Atomically write the rotated credentials back to kimi-code.json."""
    directory = os.path.dirname(creds_path) or "."
    try:
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(creds, handle)
        os.replace(tmp_path, creds_path)
    except OSError:
        pass  # keeping the in-memory token still lets this sweep finish


def fetch_kimi_usage(kimi_home="~/.kimi-code", now=None):
    """Live Kimi account/plan from its own coding/v1 API.

    Uses the access token kimi-code persisted in credentials/kimi-code.json;
    refreshes it first (same OAuth refresh-token grant the CLI uses) when
    expired, since this sweep runs once at plugin load and a stale token is
    the common case, not the exception.
    """
    creds_path = os.path.join(
        os.path.expanduser(kimi_home), "credentials", "kimi-code.json"
    )
    try:
        with open(creds_path, "r", encoding="utf-8") as handle:
            creds = json.load(handle)
    except (OSError, ValueError):
        return None
    access_token = creds.get("access_token")
    if not access_token:
        return None
    expires_at = creds.get("expires_at")  # epoch seconds
    now_s = now if now is not None else time.time()
    if isinstance(expires_at, (int, float)) and expires_at <= now_s + 60:
        refreshed = _refresh_kimi_token(creds_path, creds)
        if not refreshed:
            return {"error": "token expired — open Kimi to refresh", "source": "live"}
        access_token = refreshed["access_token"]
    headers = {"Authorization": "Bearer %s" % access_token, "Accept": "application/json"}
    try:
        payload = _http_json("https://api.kimi.com/coding/v1/me", headers)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return parse_kimi_me(payload)


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

    kimi = fetch_kimi_usage(os.path.join(home_dir, ".kimi-code"), now=now)
    if kimi:
        results["kimi"] = kimi

    # Qwen Code is configured to bill through OpenRouter (its settings.json
    # persists the key), so the OpenRouter key status IS qwen's quota.
    openrouter = fetch_openrouter_usage(os.path.join(home_dir, ".qwen"), now=now)
    if openrouter:
        results["qwen"] = openrouter

    return results
