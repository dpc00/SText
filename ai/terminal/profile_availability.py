"""Local-only availability checks for ai_terminal launch profiles.

This module deliberately performs no network requests, provider probes, OAuth,
or model inference. A configured profile is launchable when its executable
exists locally. Provider output can additionally supply a live quota update.
"""

import os
import re
import shutil


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
    low = (text or "").lower()
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

    return max(updates, key=lambda update: update[0])[1] if updates else None
