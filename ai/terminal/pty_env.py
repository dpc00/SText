"""PTY child environment sanitization (pure — no Sublime).

The Sublime plugin host (and agent shells that launched it) often carry
anti-TUI defaults: NO_COLOR=1, FORCE_COLOR=0, TERM=dumb. ConPTY children
inherit that block and apps like Grok report color=none (see `grok doctor`).

Profile spawn_env is applied last so intentional overrides still win.
"""

_DUMB_TERMS = frozenset(("", "dumb", "unknown", "none"))


def sanitize_pty_env(base_env, profile_env=None):
    """Return env for a color-capable interactive TUI child.

    Parameters
    ----------
    base_env : mapping
        Typically ``os.environ`` (or a copy).
    profile_env : mapping or None
        Profile/settings ``spawn_env`` overrides; applied after sanitization.
    """
    env = dict(base_env)
    env.pop("NO_COLOR", None)
    fc = env.get("FORCE_COLOR")
    if fc is None or fc == "" or fc == "0":
        env["FORCE_COLOR"] = "1"
    term = (env.get("TERM") or "").lower()
    if term in _DUMB_TERMS:
        env["TERM"] = "xterm-256color"
    if not env.get("COLORTERM"):
        env["COLORTERM"] = "truecolor"
    if profile_env:
        env.update(profile_env)
    return env
