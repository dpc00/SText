"""End-to-end flow tests: actually run the launcher commands.

Everything else in the suite tests the *pure* model or static structure. This
file executes the real command bodies from ``ai_terminal.py`` against a stub
Sublime API, driving the full path a user takes: open the launcher, pick an
agent, pick a folder, and assert the terminal is spawned with the right
arguments. That is the layer where the interesting bugs live (wrong argument
names, an exception inside a row builder, off-by-one on the Browse row) and
none of it is reachable from the pure tests.

Stubs are installed into ``sys.modules`` before importing ``ai_terminal``,
which cannot be imported outside Sublime otherwise.
"""

import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


# ─── stub Sublime API ────────────────────────────────────────────────────────


class Kind(tuple):
    pass


class QuickPanelItem:
    def __init__(self, trigger, details="", annotation="", kind=None):
        self.trigger = trigger
        self.details = details
        self.annotation = annotation
        self.kind = kind

    def __repr__(self):
        return "QuickPanelItem(%r, %r, %r)" % (
            self.trigger,
            self.details,
            self.annotation,
        )


class Settings(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)

    def set(self, k, v):
        self[k] = v

    def add_on_change(self, *a, **k):
        pass

    def clear_on_change(self, *a, **k):
        pass


class FakeView:
    def __init__(self, vid=1, file_name=None):
        self._id = vid
        self._file_name = file_name
        self._settings = Settings()

    def id(self):
        return self._id

    def file_name(self):
        return self._file_name

    def settings(self):
        return self._settings

    def name(self):
        return "view-%d" % self._id


class FakeWindow:
    """Records show_quick_panel / show_input_panel / run_command calls."""

    def __init__(self, folders=(), active_view=None):
        self._folders = list(folders)
        self._active_view = active_view
        self.panels = []          # [(items, on_done, kwargs)]
        self.input_panels = []    # [(caption, initial, on_done)]
        self.commands = []        # [(name, args)]

    def folders(self):
        return list(self._folders)

    def active_view(self):
        return self._active_view

    def show_quick_panel(self, items, on_done, *args, **kwargs):
        self.panels.append((items, on_done, kwargs))

    def show_input_panel(self, caption, initial, on_done, on_change, on_cancel):
        self.input_panels.append((caption, initial, on_done))

    def run_command(self, name, args=None):
        self.commands.append((name, args or {}))

    def find_view_by_id(self, vid):
        return None

    def focus_view(self, view):
        self.commands.append(("focus_view", {"id": view.id()}))


def _install_stubs():
    if "sublime" in sys.modules:
        return sys.modules["sublime"]

    m = types.ModuleType("sublime")

    class Region:
        def __init__(self, a, b=None):
            self.a, self.b = a, b if b is not None else a

    m.Region = Region
    m.Settings = Settings
    m.QuickPanelItem = QuickPanelItem
    m.load_settings = lambda name: Settings()
    m.save_settings = lambda name: None
    m.packages_path = lambda: os.path.join(REPO, ".fake-packages")
    m.cache_path = lambda: os.path.join(REPO, ".fake-cache")
    m.executable_path = lambda: "sublime_text.exe"
    m.set_timeout = lambda fn, ms=0: None
    m.set_timeout_async = lambda fn, ms=0: None
    m.cancel_timeout = lambda tok: None
    m.status_message = lambda msg: _messages.append(("status", msg))
    m.error_message = lambda msg: _messages.append(("error", msg))
    m.message_dialog = lambda msg: _messages.append(("dialog", msg))
    m.windows = lambda: []
    m.active_window = lambda: None
    m.run_command = lambda *a, **k: None
    m.version = lambda: "4169"
    m.platform = lambda: "windows"
    m.arch = lambda: "x64"
    m.expand_variables = lambda s, v: s
    m.find_resources = lambda pattern: []
    m.load_resource = lambda path: ""
    m.get_clipboard = lambda: ""
    m.DRAW_NO_OUTLINE = 256
    m.DRAW_NO_FILL = 32
    m.DRAW_EMPTY = 1
    m.PERSISTENT = 16
    m.HIDDEN = 128
    m.LAYOUT_INLINE = 0
    m.MONOSPACE_FONT = 1
    m.HOVER_TEXT = 1
    sys.modules["sublime"] = m

    sp = types.ModuleType("sublime_plugin")

    class _Base:
        def __init__(self, arg=None):
            self.window = arg
            self.view = arg

    for name in (
        "WindowCommand",
        "TextCommand",
        "ApplicationCommand",
        "EventListener",
        "ViewEventListener",
        "TextChangeListener",
    ):
        setattr(sp, name, type(name, (_Base,), {}))
    sys.modules["sublime_plugin"] = sp
    return m


_messages = []
_install_stubs()

from ai import ai_terminal  # noqa: E402
from ai.terminal import launcher  # noqa: E402


PROFILES = {
    "Claude": {"argv": ["claude"]},
    "Codex": {"argv": ["codex"]},
    "Bash": {"argv": ["bash"]},
}

ALPHA = os.path.join(REPO, "ai")          # real dirs so os.path.isdir passes
BETA = os.path.join(REPO, "tests")


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    """Fresh in-memory history and settings per test; never touch real state."""
    del _messages[:]
    store = launcher.empty_store()
    monkeypatch.setattr(ai_terminal, "_launch_store", store, raising=False)
    monkeypatch.setattr(
        ai_terminal, "_launch_store_path", lambda: str(tmp_path / "hist.json")
    )
    settings = Settings()
    settings.update({"profiles": PROFILES, "default_profile": "Claude"})
    monkeypatch.setattr(ai_terminal, "_settings", settings, raising=False)
    monkeypatch.setattr(sys.modules["sublime"], "load_settings", lambda n: settings)
    # Treat every profile as installed unless a test says otherwise.
    monkeypatch.setattr(ai_terminal, "_profile_is_available", lambda n, s=None: True)
    monkeypatch.setattr(
        ai_terminal, "_profile_availability_label", lambda n, s=None: "80% remaining"
    )
    yield store


def _launcher_cmd(window):
    return ai_terminal.AiTerminalLauncherCommand(window)


def _triggers(items):
    return [i.trigger if hasattr(i, "trigger") else i[0] for i in items]


# ─── the two-step launch flow ────────────────────────────────────────────────


def test_launcher_shows_all_profiles_first():
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run()
    assert len(win.panels) == 1
    assert sorted(_triggers(win.panels[0][0])) == ["Bash", "Claude", "Codex"]


def test_picking_an_agent_opens_the_directory_step():
    win = FakeWindow(folders=[ALPHA, BETA])
    _launcher_cmd(win).run()
    items, on_done, _ = win.panels[0]
    on_done(_triggers(items).index("Codex"))

    assert len(win.panels) == 2, "directory step did not open"
    dir_items = _triggers(win.panels[1][0])
    assert "Browse…" in dir_items
    assert os.path.basename(ALPHA) in dir_items


def test_full_flow_spawns_the_chosen_agent_in_the_chosen_folder():
    win = FakeWindow(folders=[ALPHA, BETA])
    _launcher_cmd(win).run()

    items, on_profile, _ = win.panels[0]
    on_profile(_triggers(items).index("Claude"))

    dir_items, on_dir, _ = win.panels[1]
    on_dir(_triggers(dir_items).index(os.path.basename(BETA)))

    assert win.commands, "no command was run"
    name, args = win.commands[-1]
    assert name == "ai_terminal_open_here"
    assert args["profile"] == "Claude"
    assert args["paths"] == [BETA]


def test_escape_from_directory_step_reopens_the_agent_step():
    """Esc must not drop the whole flow; it should go back one step."""
    calls = []
    win = FakeWindow(folders=[ALPHA])
    win.run_command = lambda name, args=None: calls.append(name)
    # set_timeout is a no-op stub, so invoke the scheduled callback directly.
    scheduled = []
    sys.modules["sublime"].set_timeout = lambda fn, ms=0: scheduled.append(fn)
    try:
        _launcher_cmd(win).run()
        items, on_profile, _ = win.panels[0]
        on_profile(0)
        _, on_dir, _ = win.panels[1]
        on_dir(-1)
        assert scheduled, "nothing was scheduled on cancel"
        scheduled[0]()
        assert "ai_terminal_launcher" in calls
    finally:
        sys.modules["sublime"].set_timeout = lambda fn, ms=0: None


def test_browse_row_opens_an_input_panel_and_rejects_a_bad_path():
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run()
    items, on_profile, _ = win.panels[0]
    on_profile(0)
    dir_items, on_dir, _ = win.panels[1]

    on_dir(len(dir_items) - 1)  # the Browse… row is last
    assert win.input_panels, "Browse did not open an input panel"

    _, _, on_text = win.input_panels[0]
    on_text(os.path.join(REPO, "definitely-not-a-real-dir"))
    assert any(kind == "error" for kind, _ in _messages)
    assert not win.commands, "a bad path must not spawn anything"


def test_browse_accepts_a_real_path():
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run()
    win.panels[0][1](0)
    dir_items, on_dir, _ = win.panels[1]
    on_dir(len(dir_items) - 1)
    win.input_panels[0][2](BETA)

    name, args = win.commands[-1]
    assert name == "ai_terminal_open_here" and args["paths"] == [BETA]


def test_sidebar_paths_skip_the_directory_step():
    """A right-click already answered 'where'; do not ask again."""
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run(paths=[BETA])
    assert len(win.panels) == 1, "should only ask which agent"
    win.panels[0][1](0)
    assert win.commands[-1][0] == "ai_terminal_open_here"


def test_no_profiles_falls_back_to_default_terminal(monkeypatch):
    monkeypatch.setattr(ai_terminal, "_profile_names", lambda s=None: [])
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run()
    assert not win.panels
    assert win.commands[-1][0] == "ai_terminal_open_here"


# ─── ranking actually reaches the UI ─────────────────────────────────────────


def test_history_puts_the_usual_agent_first(clean_state):
    for _ in range(3):
        launcher.record_launch(clean_state, "Codex", ALPHA)
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run()
    assert _triggers(win.panels[0][0])[0] == "Codex"


def test_history_puts_the_usual_folder_first(clean_state):
    for _ in range(3):
        launcher.record_launch(clean_state, "Claude", BETA)
    win = FakeWindow(folders=[ALPHA, BETA])
    _launcher_cmd(win).run()
    items, on_profile, _ = win.panels[0]
    on_profile(_triggers(items).index("Claude"))
    assert _triggers(win.panels[1][0])[0] == os.path.basename(BETA)


def test_rows_carry_usage_annotation_and_a_kind():
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run()
    row = win.panels[0][0][0]
    assert "80% remaining" in row.annotation
    assert row.kind and len(row.kind) == 3


def test_unavailable_profile_is_listed_but_marked(monkeypatch):
    monkeypatch.setattr(
        ai_terminal, "_profile_is_available", lambda n, s=None: n != "Codex"
    )
    win = FakeWindow(folders=[ALPHA])
    _launcher_cmd(win).run()
    rows = {r.trigger: r for r in win.panels[0][0]}
    assert "Codex" in rows, "unavailable profiles must stay visible"
    assert rows["Codex"].kind[1] == "x"


# ─── recent sessions ─────────────────────────────────────────────────────────


def test_recent_sessions_relaunches_a_finished_session(clean_state, monkeypatch):
    launcher.record_launch(clean_state, "Codex", BETA, session_id=42)
    launcher.record_close(clean_state, 42)
    monkeypatch.setattr(ai_terminal, "_term_registry", lambda: {})

    win = FakeWindow()
    ai_terminal.AiTerminalRecentSessionsCommand(win).run()
    items, on_done, _ = win.panels[0]
    assert "Codex" in items[0].trigger

    on_done(0)
    name, args = win.commands[-1]
    assert name == "ai_terminal_open_here"
    assert args["profile"] == "Codex" and args["paths"] == [BETA]


def test_recent_sessions_reports_empty_history(monkeypatch):
    monkeypatch.setattr(ai_terminal, "_term_registry", lambda: {})
    win = FakeWindow()
    ai_terminal.AiTerminalRecentSessionsCommand(win).run()
    assert not win.panels
    assert any(kind == "status" for kind, _ in _messages)


def test_recent_sessions_refuses_a_vanished_folder(clean_state, monkeypatch):
    gone = os.path.join(REPO, "no-such-folder-here")
    launcher.record_launch(clean_state, "Claude", gone, session_id=7)
    launcher.record_close(clean_state, 7)
    monkeypatch.setattr(ai_terminal, "_term_registry", lambda: {})

    win = FakeWindow()
    ai_terminal.AiTerminalRecentSessionsCommand(win).run()
    win.panels[0][1](0)
    assert any(kind == "error" for kind, _ in _messages)
    assert not win.commands, "must not relaunch into a missing folder"


def test_recent_sessions_marks_a_live_session(clean_state, monkeypatch):
    launcher.record_launch(clean_state, "Claude", ALPHA, session_id=99)
    monkeypatch.setattr(ai_terminal, "_term_registry", lambda: {99: object()})
    win = FakeWindow()
    ai_terminal.AiTerminalRecentSessionsCommand(win).run()
    assert "live" in win.panels[0][0][0].annotation


# ─── launch history is recorded ──────────────────────────────────────────────


def test_launch_is_recorded_for_next_time(clean_state):
    ai_terminal._record_launch_history("Codex", BETA, session_id=5, tab="Ai 1")
    assert launcher.profile_stats(clean_state, "Codex")["count"] == 1
    rows = launcher.recent_sessions(clean_state, live_ids={5})
    assert rows[0]["profile"] == "Codex" and rows[0]["live"] is True

    ai_terminal._record_close_history(5)
    assert launcher.recent_sessions(clean_state, live_ids=set())[0]["live"] is False


def test_history_failure_never_breaks_launching(monkeypatch):
    """A broken history store must not stop a terminal from opening."""
    def boom():
        raise IOError("disk on fire")

    monkeypatch.setattr(ai_terminal, "_get_launch_store", boom)
    ai_terminal._record_launch_history("Claude", ALPHA, session_id=1)  # must not raise


# ─── usage refresh command ───────────────────────────────────────────────────


def test_refresh_usage_forces_a_sweep(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ai_terminal, "_ensure_usage_scanner", lambda force=False: calls.append(force)
    )
    ai_terminal.AiTerminalRefreshUsageCommand(FakeWindow()).run()
    assert calls == [True]
