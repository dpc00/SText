"""Local-only availability checks for ai_terminal launch profiles.

This module deliberately performs no network requests, provider probes, OAuth,
or model inference.  A profile is usable when it is explicitly allowed by the
settings allowlist and its launch executable exists locally.
"""

import os
import shutil


def command_exists(argv, path=None):
    """Return whether the first argv item can be launched on this machine."""
    if not isinstance(argv, (list, tuple)) or not argv or not isinstance(argv[0], str):
        return False
    executable = os.path.expandvars(os.path.expanduser(argv[0]))
    if os.path.isabs(executable) or os.path.dirname(executable):
        return os.path.isfile(executable)
    return shutil.which(executable, path=path) is not None


def profile_is_available(name, profile, available_profiles=None, path=None):
    """Return local availability for one named profile.

    ``available_profiles`` is the user's explicit statement that the profile's
    login/subscription is ready.  Omitting the setting preserves backward
    compatibility and falls back to executable detection only.
    """
    if not name or not isinstance(profile, dict):
        return False
    if isinstance(available_profiles, list) and name not in available_profiles:
        return False
    return command_exists(profile.get("launch_command"), path=path)
