"""Frecency-ranked launcher model for ai_terminal (pure, unit-testable).

The launcher answers two questions the old menus answered badly:

1. *Which agent?*  A flat alphabetical profile list buries the two or three
   CLIs actually used today under a dozen that are installed but idle.
2. *Which directory?*  The old flow re-derived candidates from the sidebar on
   every launch and re-asked even when the same repo had been chosen twenty
   times in a row.

Both are solved with the same **frecency** score (frequency x recency, the
Firefox awesomebar model): every launch is recorded, and each entry decays
with a half-life so a burst of use three weeks ago cannot outrank steady use
today. Scores only *reorder* rows — nothing is ever hidden, so a rarely used
profile is still one keystroke of filter text away.

No Sublime imports: ai_terminal supplies paths/settings and renders the rows.
"""

import json
import math
import os
import time


# Half-life for the recency decay, in seconds. At 10 days, a launch from
# ~1.5 months ago carries roughly an eighth of the weight of one today, so
# the list tracks the current project without thrashing on a single use.
HALF_LIFE_SECONDS = 10 * 24 * 3600

# Cap on retained history entries per section, so the store cannot grow
# without bound in a long-lived profile directory.
MAX_ENTRIES = 200

STORE_VERSION = 1


# ─── store I/O ───────────────────────────────────────────────────────────────

# Number of individual launches kept in the session log. The log is what the
# "Recent Sessions" picker reads: unlike the aggregated frecency sections it
# preserves each launch separately, so a repeated agent/directory pair appears
# once per use with its own timestamp.
MAX_SESSIONS = 100


def empty_store():
    return {
        "version": STORE_VERSION,
        "profiles": {},
        "dirs": {},
        "pairs": {},
        "sessions": [],
    }


def load_store(path):
    """Read the frecency store; never raises, returns an empty store instead.

    A corrupt or partially written store must not be able to break launching,
    which is why every failure mode collapses to "no history yet".
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return empty_store()
    if not isinstance(data, dict):
        return empty_store()
    store = empty_store()
    for key in ("profiles", "dirs", "pairs"):
        section = data.get(key)
        if isinstance(section, dict):
            store[key] = {
                k: v for k, v in section.items()
                if isinstance(k, str) and isinstance(v, dict)
            }
    sessions = data.get("sessions")
    if isinstance(sessions, list):
        store["sessions"] = [s for s in sessions if isinstance(s, dict)][-MAX_SESSIONS:]
    return store


def save_store(path, store):
    """Write the store atomically. Returns True on success."""
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# ─── scoring ─────────────────────────────────────────────────────────────────

def _entry(section, key):
    entry = section.get(key)
    return entry if isinstance(entry, dict) else None


def score_entry(entry, now=None):
    """Frecency score for one history entry (0.0 when unknown)."""
    if not entry:
        return 0.0
    now = time.time() if now is None else now
    count = entry.get("count") or 0
    last = entry.get("last") or 0
    try:
        count = float(count)
        last = float(last)
    except (TypeError, ValueError):
        return 0.0
    if count <= 0:
        return 0.0
    age = max(0.0, now - last)
    decay = math.pow(0.5, age / HALF_LIFE_SECONDS)
    # log1p keeps a heavily used entry from permanently pinning the top slot:
    # ten launches beats three, but not by ten-to-three.
    return math.log1p(count) * decay


def record_launch(store, profile, path, now=None, session_id=None, tab=None):
    """Record one launch of *profile* in *path*, returning the mutated store.

    Updates both the aggregated frecency sections (which drive ordering) and
    the append-only session log (which drives the "Recent Sessions" list).
    """
    now = time.time() if now is None else now

    def bump(section, key):
        if not key:
            return
        entry = _entry(section, key) or {"count": 0, "last": 0}
        entry["count"] = (entry.get("count") or 0) + 1
        entry["last"] = now
        section[key] = entry
        _trim(section)

    bump(store.setdefault("profiles", {}), profile)
    bump(store.setdefault("dirs", {}), _norm_key(path))
    if profile and path:
        bump(store.setdefault("pairs", {}), pair_key(profile, path))

    sessions = store.setdefault("sessions", [])
    sessions.append({
        "id": session_id,
        "profile": profile,
        "path": path,
        "tab": tab,
        "started": now,
        "ended": None,
    })
    del sessions[:-MAX_SESSIONS]
    return store


def record_close(store, session_id, now=None):
    """Stamp the end time on a logged session so its duration can be shown."""
    if session_id is None:
        return store
    now = time.time() if now is None else now
    for session in reversed(store.get("sessions") or []):
        if session.get("id") == session_id:
            if not session.get("ended"):
                session["ended"] = now
            break
    return store


def recent_sessions(store, limit=25, exists=os.path.isdir, live_ids=None):
    """Session log, newest first, annotated with liveness and directory state.

    Each row is the raw logged dict plus:
      ``live``   — the session's tab is still open
      ``missing``— the directory no longer exists, so relaunch would fail

    A missing end stamp is not enough to call a session live: Sublime can exit
    without firing on_close, which would otherwise leave ghosts marked live
    forever. When the caller passes ``live_ids`` (the ids actually in the
    terminal registry) that set is authoritative.
    """
    rows = []
    for session in reversed(store.get("sessions") or []):
        path = session.get("path")
        row = dict(session)
        if live_ids is None:
            row["live"] = not session.get("ended")
        else:
            row["live"] = session.get("id") in live_ids
        row["missing"] = bool(path) and not exists(path)
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def format_duration(seconds):
    """Compact human duration, e.g. '45s', '12m', '3h 20m'."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        return ""
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    hours, minutes = divmod(seconds // 60, 60)
    return "%dh %dm" % (hours, minutes) if minutes else "%dh" % hours


def session_detail(session, now=None, home=None):
    """Right-hand detail line for one session row in the quick panel."""
    now = time.time() if now is None else now
    started = session.get("started") or 0
    parts = [shorten_path(session.get("path"), home=home)]
    if session.get("live"):
        parts.append("live · %s" % format_duration(now - started))
    else:
        ended = session.get("ended") or started
        span = format_duration(ended - started)
        parts.append("%s · ran %s" % (relative_age(started, now), span) if span
                     else relative_age(started, now))
    if session.get("missing"):
        parts.append("folder missing")
    return "  ".join(p for p in parts if p)


def _trim(section):
    if len(section) <= MAX_ENTRIES:
        return
    ordered = sorted(section.items(), key=lambda kv: kv[1].get("last") or 0)
    for key, _ in ordered[: len(section) - MAX_ENTRIES]:
        section.pop(key, None)


def _norm_key(path):
    if not path:
        return ""
    return os.path.normcase(os.path.abspath(path))


def pair_key(profile, path):
    return "%s\x1f%s" % (profile, _norm_key(path))


def rank_profiles(names, store, now=None, path=None):
    """Order profile names by frecency, then alphabetically.

    When *path* is given, launches of that profile **in that directory** are
    weighted more heavily, so opening the launcher inside a repo surfaces the
    agent normally used for that repo rather than the globally most used one.
    """
    profiles = store.get("profiles", {})
    pairs = store.get("pairs", {})

    def key(name):
        score = score_entry(_entry(profiles, name), now)
        if path:
            score += 2.0 * score_entry(_entry(pairs, pair_key(name, path)), now)
        return (-score, name.lower())

    return sorted(names, key=key)


def rank_dirs(paths, store, now=None, profile=None):
    """Order candidate directories by frecency, then by basename."""
    dirs = store.get("dirs", {})
    pairs = store.get("pairs", {})

    def key(path):
        score = score_entry(_entry(dirs, _norm_key(path)), now)
        if profile:
            score += 2.0 * score_entry(_entry(pairs, pair_key(profile, path)), now)
        return (-score, os.path.basename(path.rstrip("\\/")).lower(), path.lower())

    return sorted(paths, key=key)


def recent_dirs(store, limit=8, now=None, exists=os.path.isdir):
    """Most-frecent directories from history that still exist on disk."""
    dirs = store.get("dirs", {})
    scored = [
        (score_entry(entry, now), key)
        for key, entry in dirs.items()
        if key and exists(key)
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [key for _, key in scored[:limit]]


def last_pair(store, now=None, exists=os.path.isdir):
    """The most recently launched (profile, path) pair still on disk, or None."""
    best = None
    best_at = -1.0
    for key, entry in (store.get("pairs") or {}).items():
        if "\x1f" not in key:
            continue
        profile, path = key.split("\x1f", 1)
        last = entry.get("last") or 0
        try:
            last = float(last)
        except (TypeError, ValueError):
            continue
        if last > best_at and exists(path):
            best = (profile, path)
            best_at = last
    return best


# ─── row presentation ────────────────────────────────────────────────────────

# Sublime kind tuples are (KIND_ID, letter, display-name). The numeric ids are
# inlined so this module stays importable without Sublime for tests.
KIND_ID_AMBIGUOUS = 0
KIND_ID_KEYWORD = 1
KIND_ID_TYPE = 2
KIND_ID_FUNCTION = 3
KIND_ID_NAMESPACE = 4
KIND_ID_NAVIGATION = 5
KIND_ID_MARKUP = 6
KIND_ID_VARIABLE = 7
KIND_ID_SNIPPET = 8

# Shells are deliberately a different colour/letter from agents so the two
# groups are separable at a glance even though they share one list.
SHELL_PROFILES = ("Bash", "PowerShell", "Dos Console", "WSL Bash")


def profile_kind(name, available=True, exhausted=False, shells=SHELL_PROFILES):
    """(kind_id, letter, label) describing one profile row."""
    if not available:
        return (KIND_ID_AMBIGUOUS, "x", "Not installed")
    if exhausted:
        return (KIND_ID_VARIABLE, "!", "Quota exhausted")
    if name in shells:
        return (KIND_ID_NAMESPACE, "$", "Shell")
    return (KIND_ID_FUNCTION, "A", "Agent")


def dir_kind(is_recent=False, is_git=False):
    if is_recent:
        return (KIND_ID_SNIPPET, "R", "Recent")
    if is_git:
        return (KIND_ID_NAVIGATION, "G", "Repository")
    return (KIND_ID_TYPE, "D", "Folder")


def shorten_path(path, home=None):
    """Display form for a path: ~ for home, and no trailing separator."""
    if not path:
        return ""
    home = os.path.expanduser("~") if home is None else home
    text = path.rstrip("\\/") or path
    if home:
        home_n = os.path.normcase(home.rstrip("\\/"))
        text_n = os.path.normcase(text)
        if text_n == home_n:
            return "~"
        if text_n.startswith(home_n + os.sep):
            return "~" + text[len(home):]
    return text


def relative_age(seconds, now=None):
    """Compact age string for a timestamp, e.g. 'just now', '3m ago', '5d ago'."""
    if not seconds:
        return "never"
    now = time.time() if now is None else now
    delta = max(0, int(now - seconds))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return "%dm ago" % (delta // 60)
    if delta < 86400:
        return "%dh ago" % (delta // 3600)
    return "%dd ago" % (delta // 86400)


def session_kind(session):
    """(kind_id, letter, label) for one row of the recent-sessions list."""
    if session.get("missing"):
        return (KIND_ID_AMBIGUOUS, "x", "Folder missing")
    if session.get("live"):
        return (KIND_ID_SNIPPET, "*", "Live")
    return (KIND_ID_NAVIGATION, "H", "History")


# The Browse… escape hatch reads as an action, not a folder, so it gets its own
# kind rather than borrowing dir_kind.
BROWSE_KIND = (KIND_ID_KEYWORD, "+", "Browse")


def session_title(session):
    """Primary line of a session row: the agent, then the folder's base name."""
    profile = session.get("profile") or "terminal"
    path = session.get("path") or ""
    base = os.path.basename(path.rstrip("\\/")) or path
    return "%s — %s" % (profile, base) if base else profile


def session_annotation(session, now=None):
    """Right-aligned session state: 'live 12m', or how long ago it ended."""
    if session.get("missing"):
        return "missing"
    started = session.get("started") or 0
    if session.get("live"):
        now = time.time() if now is None else now
        return "live · %s" % format_duration(max(0, now - started))
    return relative_age(session.get("ended") or started, now=now)


def profile_stats(store, name):
    """Aggregates shown under a profile row: launch count, recency, last folder.

    ``last_dir`` is read from the pair section rather than the session log so it
    stays correct after the log rolls over past MAX_SESSIONS entries.
    """
    entry = _entry(store.get("profiles", {}), name) or {}
    last_dir = None
    best = -1.0
    for key, pair in (store.get("pairs") or {}).items():
        if "\x1f" not in key:
            continue
        profile, path = key.split("\x1f", 1)
        if profile != name:
            continue
        last = pair.get("last") or 0
        if last > best:
            best, last_dir = last, path
    return {
        "count": entry.get("count") or 0,
        "last": entry.get("last") or 0,
        "last_dir": last_dir,
    }


def dir_stats(store, path):
    """Aggregates shown under a directory row: use count, recency, last agent."""
    entry = _entry(store.get("dirs", {}), _norm_key(path)) or {}
    key_n = _norm_key(path)
    last_profile = None
    best = -1.0
    for key, pair in (store.get("pairs") or {}).items():
        if "\x1f" not in key:
            continue
        profile, pair_path = key.split("\x1f", 1)
        if _norm_key(pair_path) != key_n:
            continue
        last = pair.get("last") or 0
        if last > best:
            best, last_profile = last, profile
    return {
        "count": entry.get("count") or 0,
        "last": entry.get("last") or 0,
        "last_profile": last_profile,
    }


def dir_candidates(store, extra=(), now=None, exists=os.path.isdir):
    """Known directories: frecent history first, then any unseen *extra* folder.

    De-duplicated case-insensitively so an open sidebar folder that is also in
    history appears once, keeping its history ranking.
    """
    ordered = list(recent_dirs(store, limit=MAX_ENTRIES, now=now, exists=exists))
    seen = {_norm_key(p) for p in ordered}
    for path in extra or ():
        if path and _norm_key(path) not in seen:
            seen.add(_norm_key(path))
            ordered.append(path)
    return ordered
