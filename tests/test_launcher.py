"""Tests for the frecency launcher model (pure, no Sublime)."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai.terminal import launcher  # noqa: E402


NOW = 1_800_000_000.0
DAY = 86400.0


def test_load_store_missing_file_is_empty(tmp_path):
    store = launcher.load_store(str(tmp_path / "nope.json"))
    assert store["profiles"] == {} and store["sessions"] == []


def test_load_store_corrupt_file_is_empty(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    assert launcher.load_store(str(path)) == launcher.empty_store()


def test_save_then_load_roundtrip(tmp_path):
    path = str(tmp_path / "sub" / "s.json")
    store = launcher.record_launch(
        launcher.empty_store(), "Claude", str(tmp_path), now=NOW, session_id=7
    )
    assert launcher.save_store(path, store)
    loaded = launcher.load_store(path)
    assert loaded["profiles"]["Claude"]["count"] == 1
    assert loaded["sessions"][0]["id"] == 7


def test_score_decays_with_age():
    fresh = {"count": 1, "last": NOW}
    old = {"count": 1, "last": NOW - launcher.HALF_LIFE_SECONDS}
    assert launcher.score_entry(fresh, NOW) > launcher.score_entry(old, NOW)
    assert launcher.score_entry(old, NOW) == launcher.score_entry(fresh, NOW) / 2


def test_score_unknown_entry_is_zero():
    assert launcher.score_entry(None, NOW) == 0.0
    assert launcher.score_entry({"count": 0, "last": NOW}, NOW) == 0.0
    assert launcher.score_entry({"count": "x", "last": NOW}, NOW) == 0.0


def test_rank_profiles_puts_used_first_then_alphabetical():
    store = launcher.empty_store()
    launcher.record_launch(store, "Codex", "/p", now=NOW)
    ranked = launcher.rank_profiles(["Zed", "Codex", "Alpha"], store, now=NOW)
    assert ranked == ["Codex", "Alpha", "Zed"]


def test_rank_profiles_prefers_agent_used_in_this_directory():
    store = launcher.empty_store()
    # Claude is used more globally, but Codex is what this repo uses.
    for _ in range(5):
        launcher.record_launch(store, "Claude", "/other", now=NOW)
    launcher.record_launch(store, "Codex", "/repo", now=NOW)
    ranked = launcher.rank_profiles(
        ["Claude", "Codex"], store, now=NOW, path="/repo"
    )
    assert ranked[0] == "Codex"
    assert launcher.rank_profiles(["Claude", "Codex"], store, now=NOW)[0] == "Claude"


def test_rank_dirs_orders_by_frecency_then_basename():
    store = launcher.empty_store()
    launcher.record_launch(store, "Claude", "/a/used", now=NOW)
    ranked = launcher.rank_dirs(["/a/zeta", "/a/used", "/a/beta"], store, now=NOW)
    assert ranked == ["/a/used", "/a/beta", "/a/zeta"]


def test_recent_dirs_skips_paths_that_no_longer_exist():
    store = launcher.empty_store()
    launcher.record_launch(store, "Claude", "/gone", now=NOW)
    launcher.record_launch(store, "Claude", "/here", now=NOW)
    got = launcher.recent_dirs(
        store, now=NOW, exists=lambda p: "here" in p
    )
    assert [os.path.basename(p) for p in got] == ["here"]


def test_last_pair_returns_most_recent_existing():
    store = launcher.empty_store()
    launcher.record_launch(store, "Claude", "/one", now=NOW - DAY)
    launcher.record_launch(store, "Codex", "/two", now=NOW)
    profile, path = launcher.last_pair(store, exists=lambda p: True)
    assert profile == "Codex"
    assert path.endswith("two")


def test_last_pair_is_none_without_history():
    assert launcher.last_pair(launcher.empty_store()) is None


def test_trim_keeps_newest_entries():
    store = launcher.empty_store()
    for i in range(launcher.MAX_ENTRIES + 25):
        launcher.record_launch(store, "P%d" % i, "/d%d" % i, now=NOW + i)
    assert len(store["profiles"]) == launcher.MAX_ENTRIES
    assert "P0" not in store["profiles"]
    assert "P%d" % (launcher.MAX_ENTRIES + 24) in store["profiles"]


# ─── session log ─────────────────────────────────────────────────────────────

def test_sessions_record_each_launch_separately():
    store = launcher.empty_store()
    launcher.record_launch(store, "Claude", "/repo", now=NOW, session_id=1)
    launcher.record_launch(store, "Claude", "/repo", now=NOW + 60, session_id=2)
    rows = launcher.recent_sessions(store, exists=lambda p: True)
    assert [r["id"] for r in rows] == [2, 1]  # newest first


def test_sessions_are_capped():
    store = launcher.empty_store()
    for i in range(launcher.MAX_SESSIONS + 10):
        launcher.record_launch(store, "P", "/d", now=NOW + i, session_id=i)
    assert len(store["sessions"]) == launcher.MAX_SESSIONS
    assert store["sessions"][0]["id"] == 10


def test_record_close_stamps_end_once():
    store = launcher.empty_store()
    launcher.record_launch(store, "Claude", "/repo", now=NOW, session_id=5)
    launcher.record_close(store, 5, now=NOW + 100)
    launcher.record_close(store, 5, now=NOW + 999)
    assert store["sessions"][0]["ended"] == NOW + 100


def test_record_close_unknown_id_is_noop():
    store = launcher.empty_store()
    launcher.record_launch(store, "Claude", "/repo", now=NOW, session_id=5)
    launcher.record_close(store, 404, now=NOW + 100)
    assert store["sessions"][0]["ended"] is None


def test_recent_sessions_flags_live_and_missing():
    store = launcher.empty_store()
    launcher.record_launch(store, "Claude", "/gone", now=NOW, session_id=1)
    launcher.record_launch(store, "Codex", "/here", now=NOW, session_id=2)
    launcher.record_close(store, 1, now=NOW + 10)
    rows = {r["id"]: r for r in launcher.recent_sessions(
        store, exists=lambda p: "here" in p)}
    assert rows[1]["missing"] is True and rows[1]["live"] is False
    assert rows[2]["missing"] is False and rows[2]["live"] is True


def test_format_duration():
    assert launcher.format_duration(45) == "45s"
    assert launcher.format_duration(600) == "10m"
    assert launcher.format_duration(3600) == "1h"
    assert launcher.format_duration(3600 * 3 + 1200) == "3h 20m"
    assert launcher.format_duration(None) == ""


def test_session_detail_live_and_finished():
    live = {"path": "/repo", "started": NOW - 300, "live": True}
    assert "live" in launcher.session_detail(live, now=NOW)
    done = {"path": "/repo", "started": NOW - 7200, "ended": NOW - 3600}
    detail = launcher.session_detail(done, now=NOW)
    assert "ran 1h" in detail and "ago" in detail


def test_session_detail_notes_missing_folder():
    row = {"path": "/gone", "started": NOW, "missing": True}
    assert "folder missing" in launcher.session_detail(row, now=NOW)


# ─── presentation ────────────────────────────────────────────────────────────

def test_profile_kind_distinguishes_states():
    assert launcher.profile_kind("Claude")[2] == "Agent"
    assert launcher.profile_kind("Bash")[2] == "Shell"
    assert launcher.profile_kind("Claude", available=False)[2] == "Not installed"
    assert launcher.profile_kind("Claude", exhausted=True)[2] == "Quota exhausted"


def test_unavailable_beats_exhausted_in_kind():
    kind = launcher.profile_kind("Claude", available=False, exhausted=True)
    assert kind[2] == "Not installed"


def test_shorten_path_uses_tilde():
    home = os.path.normpath("/home/d")
    assert launcher.shorten_path(home, home=home) == "~"
    child = os.path.join(home, "projects")
    assert launcher.shorten_path(child, home=home) == "~" + os.sep + "projects"
    assert launcher.shorten_path("/elsewhere/x", home=home) == "/elsewhere/x"


def test_shorten_path_empty():
    assert launcher.shorten_path("") == ""


def test_relative_age_buckets():
    assert launcher.relative_age(0) == "never"
    assert launcher.relative_age(NOW - 5, NOW) == "just now"
    assert launcher.relative_age(NOW - 300, NOW) == "5m ago"
    assert launcher.relative_age(NOW - 7200, NOW) == "2h ago"
    assert launcher.relative_age(NOW - 3 * DAY, NOW) == "3d ago"


def test_session_kind_states():
    assert launcher.session_kind({"live": True})[2] == "Live"
    assert launcher.session_kind({"missing": True})[2] == "Folder missing"
    assert launcher.session_kind({})[2] == "History"


def test_dir_kind_states():
    assert launcher.dir_kind(is_recent=True)[2] == "Recent"
    assert launcher.dir_kind(is_git=True)[2] == "Repository"
    assert launcher.dir_kind()[2] == "Folder"


def test_scores_use_wall_clock_by_default():
    store = launcher.record_launch(launcher.empty_store(), "X", "/d")
    assert launcher.score_entry(store["profiles"]["X"]) > 0
    assert abs(store["profiles"]["X"]["last"] - time.time()) < 5
