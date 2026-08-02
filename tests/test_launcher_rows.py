"""Tests for the launcher's row-presentation helpers (pure, no Sublime).

These back the quick-panel rows: what each row says, how it is ranked, and how
liveness is decided. Kept separate from test_launcher.py, which covers the
frecency math and the on-disk store.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai.terminal import launcher  # noqa: E402


NOW = 1_800_000_000.0
DAY = 86400.0

ALPHA = os.path.join("C:", os.sep, "proj", "alpha")
BETA = os.path.join("C:", os.sep, "proj", "beta")
GAMMA = os.path.join("C:", os.sep, "proj", "gamma")


def _store_with_history():
    store = launcher.empty_store()
    launcher.record_launch(store, "Claude", ALPHA, now=NOW - DAY, session_id=1)
    launcher.record_launch(store, "Claude", ALPHA, now=NOW, session_id=2)
    launcher.record_launch(store, "Codex", BETA, now=NOW - 2 * DAY, session_id=3)
    return store


# ─── aggregates shown under each row ─────────────────────────────────────────


def test_profile_stats_counts_and_last_dir():
    stats = launcher.profile_stats(_store_with_history(), "Claude")
    assert stats["count"] == 2
    assert stats["last"] == NOW
    assert stats["last_dir"].endswith("alpha")


def test_profile_stats_unknown_profile_is_zeroed():
    assert launcher.profile_stats(launcher.empty_store(), "Nope") == {
        "count": 0,
        "last": 0,
        "last_dir": None,
    }


def test_dir_stats_reports_last_profile():
    stats = launcher.dir_stats(_store_with_history(), BETA)
    assert stats["count"] == 1
    assert stats["last_profile"] == "Codex"


def test_dir_stats_is_case_insensitive_on_path():
    assert launcher.dir_stats(_store_with_history(), ALPHA.upper())["count"] == 2


# ─── directory candidate assembly ────────────────────────────────────────────


def test_dir_candidates_appends_unseen_extras():
    out = launcher.dir_candidates(
        _store_with_history(), extra=[GAMMA], now=NOW, exists=lambda p: True
    )
    assert GAMMA in out


def test_dir_candidates_dedupes_extra_already_in_history():
    out = launcher.dir_candidates(
        _store_with_history(), extra=[ALPHA.upper()], now=NOW, exists=lambda p: True
    )
    lowered = [p.lower() for p in out]
    assert lowered.count(ALPHA.lower()) == 1


def test_dir_candidates_drops_vanished_history_dirs():
    out = launcher.dir_candidates(
        _store_with_history(), now=NOW, exists=lambda p: p != ALPHA
    )
    assert ALPHA not in out


# ─── liveness ────────────────────────────────────────────────────────────────


def test_recent_sessions_live_ids_override_missing_end_stamp():
    rows = launcher.recent_sessions(
        _store_with_history(), live_ids={2}, exists=lambda p: True
    )
    live = {r["id"]: r["live"] for r in rows}
    assert live == {1: False, 2: True, 3: False}


def test_recent_sessions_without_live_ids_falls_back_to_end_stamp():
    store = _store_with_history()
    launcher.record_close(store, 1, now=NOW)
    live = {
        r["id"]: r["live"]
        for r in launcher.recent_sessions(store, exists=lambda p: True)
    }
    assert live[1] is False and live[2] is True


def test_recent_sessions_flags_vanished_directory():
    rows = launcher.recent_sessions(
        _store_with_history(), live_ids=set(), exists=lambda p: p != BETA
    )
    missing = {r["id"]: r["missing"] for r in rows}
    assert missing[3] is True and missing[1] is False


# ─── row text ────────────────────────────────────────────────────────────────


def test_session_title_pairs_agent_and_folder():
    title = launcher.session_title({"profile": "Claude", "path": ALPHA})
    assert title.startswith("Claude") and title.endswith("alpha")


def test_session_title_without_path_is_just_the_agent():
    assert launcher.session_title({"profile": "Codex", "path": ""}) == "Codex"


def test_session_annotation_live_shows_duration():
    out = launcher.session_annotation({"live": True, "started": NOW - 120}, now=NOW)
    assert "live" in out and "2m" in out


def test_session_annotation_missing_wins_over_live():
    assert launcher.session_annotation({"missing": True, "live": True}) == "missing"


def test_session_annotation_finished_uses_end_time():
    out = launcher.session_annotation(
        {"live": False, "started": NOW - 4 * DAY, "ended": NOW - DAY}, now=NOW
    )
    assert out == "1d ago"


# ─── kinds ───────────────────────────────────────────────────────────────────


def test_browse_kind_is_distinct_from_dir_kinds():
    assert launcher.BROWSE_KIND != launcher.dir_kind(is_recent=True)
    assert launcher.BROWSE_KIND != launcher.dir_kind()


def test_profile_kind_marks_exhausted_quota():
    assert "Quota" in launcher.profile_kind("Claude", exhausted=True)[2]


def test_profile_kind_unavailable_beats_exhausted():
    assert launcher.profile_kind("Claude", available=False, exhausted=True)[1] == "x"
