"""Local-only availability checks for ai_terminal launch profiles.

This module deliberately performs no network requests, provider probes, OAuth,
or model inference. A configured profile is launchable when its executable
exists locally. Provider output can additionally supply a live quota update.
"""

import os
import re
import shutil


_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _plain_terminal_text(text):
    return _ANSI_ESCAPE_RE.sub("", text or "").replace("\r", "")


def command_exists(argv, path=None):
    """Return whether the first argv item can be launched on this machine."""
    if not isinstance(argv, (list, tuple)) or not argv or not isinstance(argv[0], str):
        return False
    executable = os.path.expandvars(os.path.expanduser(argv[0]))
    if os.path.isabs(executable) or os.path.dirname(executable):
        return os.path.isfile(executable)
    return shutil.which(executable, path=path) is not None


def profile_is_available(name, profile, path=None):
    """Return whether one configured profile has a launchable executable."""
    if not name or not isinstance(profile, dict):
        return False
    return command_exists(profile.get("launch_command"), path=path)


_EXHAUSTED_PATTERNS = (
    "no credits remaining",
    "no credit remaining",
    "exhausted your daily quota",
    "exhausted your weekly quota",
    "exhausted your monthly quota",
    "exceeded your current quota",
    "usage limit reached",
    "requires more credits",
    "quota has been exhausted",
    "quota is exhausted",
)


def usage_update_from_text(text):
    """Extract a live usage update from provider-rendered terminal text.

    Returns ``None`` when the text says nothing trustworthy about availability,
    ``0.0`` for confirmed exhaustion, or a 0..100 remaining percentage when a
    provider reports one. Generic transient rate-limit messages are deliberately
    ignored because they do not prove that the account quota is empty.
    """
    low = _plain_terminal_text(text).lower()
    updates = []
    for pattern in _EXHAUSTED_PATTERNS:
        start = 0
        while True:
            index = low.find(pattern, start)
            if index < 0:
                break
            updates.append((index, 0.0))
            start = index + len(pattern)

    percentage_patterns = (
        r"(\d+(?:\.\d+)?)\s*%\s*(?:remaining|left)",
        r"(?:remaining|left)\s*[:=-]?\s*(\d+(?:\.\d+)?)\s*%",
    )
    for pattern in percentage_patterns:
        for match in re.finditer(pattern, low):
            remaining = max(0.0, min(100.0, float(match.group(1))))
            updates.append((match.start(), remaining))

    # "100% used" (Kimi CLI's weekly-limit screen, among others) is an
    # objective measurement, not a proxy for a possibly-transient rate limit,
    # so it is safe to trust the same as an explicit remaining/left figure.
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*%\s*used", low):
        remaining = max(0.0, min(100.0, 100.0 - float(match.group(1))))
        updates.append((match.start(), remaining))

    return max(updates, key=lambda update: update[0])[1] if updates else None


def menu_caption(name, remaining=None, reset=None, executable_ok=True):
    """Compact menu caption for one profile: name plus locally known status.

    Pure formatting only — callers supply state observed from real terminal
    output (never a provider probe). Returns just the name when nothing is
    known, so menus stay clean until a status is actually observed.
    """
    if not executable_ok:
        return "%s — not installed" % name
    if remaining == 0.0:
        suffix = "quota exhausted"
        if reset:
            suffix += ", resets " + reset
        return "%s — %s" % (name, suffix)
    parts = []
    if isinstance(remaining, (int, float)):
        parts.append("%g%% left" % remaining)
    if reset:
        parts.append("resets " + reset)
    return "%s — %s" % (name, ", ".join(parts)) if parts else name


def reset_update_from_text(text):
    """Return the latest provider-reported quota reset description, if any."""
    plain = _plain_terminal_text(text)
    patterns = (
        r"\b(?:quota|usage|limit)?\s*resets?\s+in\s+([^\n|]{1,80})",
        r"\b(?:quota|usage|limit)?\s*resets?\s+(?:at|on)\s+([^\n|]{1,80})",
        r"\bnext\s+reset\s*[:=-]\s*([^\n|]{1,80})",
    )
    matches = []
    for pattern in patterns:
        for match in re.finditer(pattern, plain, flags=re.IGNORECASE):
            value = match.group(1).strip(" .;,-")
            if value:
                matches.append((match.start(), value))
    return max(matches, key=lambda update: update[0])[1] if matches else None
