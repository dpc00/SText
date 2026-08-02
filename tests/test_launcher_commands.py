"""Smoke tests for the launcher commands against a stub Sublime API.

`ai_terminal.py` cannot be imported outside Sublime (it touches ctypes/conpty at
import time in ways that vary by host), so these tests exercise the *command
logic* by rebuilding the small pure parts the commands depend on and asserting
the launcher API is called the way `ai_terminal.py` calls it. That keeps the
signatures honest: every call in the plugin has a matching call here, so a
rename in launcher.py fails the suite rather than failing silently at runtime
inside Sublime, where the only symptom is a quiet console traceback.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai.terminal import launcher  # noqa: E402


ALPHA = os.path.join("C:", os.sep, "proj", "alpha")
BETA = os.path.join("C:", os.sep, "proj", "beta")
NOW = 1_800_000_000.0


def _store():
    s = launcher.empty_store()
    launcher.record_launch(s, "Claude", ALPHA, now=NOW - 100, session_id=1)
    launcher.record_launch(s, "Codex", BETA, now=NOW, session_id=2)
    return s


# ─── the exact call shapes used by ai_terminal.py ────────────────────────────


def test_profile_items_call_shape():
    """Mirrors _profile_items in ai_terminal.py."""
    store = _store()
    names = ["Claude", "Codex", "Bash"]
    ordered = launcher.rank_profiles(names, store, path=ALPHA)
    assert set(ordered) == set(names)
    for name in ordered:
        stats = launcher.profile_stats(store, name)
        launcher.profile_kind(name, available=True, exhausted=False)
        launcher.relative_age(stats["last"])
        launcher.shorten_path(stats["last_dir"])


def test_dir_items_call_shape():
    """Mirrors _dir_items in ai_terminal.py."""
    store = _store()
    candidates = launcher.dir_candidates(store, extra=[BETA], exists=lambda p: True)
    ranked = launcher.rank_dirs(candidates, store, profile="Claude")
    assert set(ranked) == set(candidates)
    for path in ranked:
        stats = launcher.dir_stats(store, path)
        launcher.dir_kind(is_recent=bool(stats["count"]), is_git=False)
        launcher.shorten_path(path)


def test_recent_sessions_call_shape():
    """Mirrors AiTerminalRecentSessionsCommand in ai_terminal.py."""
    rows = launcher.recent_sessions(
        _store(), live_ids={2}, limit=40, exists=lambda p: True
    )
    for sess in rows:
        assert isinstance(launcher.session_title(sess), str)
        assert isinstance(launcher.session_detail(sess), str)
        assert isinstance(launcher.session_annotation(sess), str)
        assert len(launcher.session_kind(sess)) == 3
        # The command reads these keys off the row to relaunch.
        assert "id" in sess and "profile" in sess and "path" in sess


# ─── behaviour the pickers promise ───────────────────────────────────────────


def test_context_directory_promotes_its_usual_agent():
    """Row 0 in the agent picker should be the agent used in this folder."""
    store = launcher.empty_store()
    launcher.record_launch(store, "Codex", BETA, now=NOW)
    launcher.record_launch(store, "Codex", BETA, now=NOW)
    launcher.record_launch(store, "Claude", ALPHA, now=NOW)
    ranked = launcher.rank_profiles(["Claude", "Codex"], store, now=NOW, path=ALPHA)
    assert ranked[0] == "Claude"


def test_chosen_agent_promotes_its_usual_directory():
    """Row 0 in the folder picker should be where that agent normally runs."""
    store = launcher.empty_store()
    launcher.record_launch(store, "Claude", ALPHA, now=NOW)
    launcher.record_launch(store, "Codex", BETA, now=NOW)
    launcher.record_launch(store, "Codex", BETA, now=NOW)
    ranked = launcher.rank_dirs([ALPHA, BETA], store, now=NOW, profile="Claude")
    assert ranked[0] == ALPHA


def test_browse_row_index_is_past_the_ranked_dirs():
    """The command treats idx >= len(ranked) as Browse…; keep that unambiguous."""
    store = _store()
    candidates = launcher.dir_candidates(store, exists=lambda p: True)
    rows = list(candidates) + ["Browse…"]
    assert rows.index("Browse…") == len(candidates)
