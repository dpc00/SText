"""Import ai_terminal the way Sublime does, with a stub sublime API.

Purpose: catch import-time and class-definition-time errors that would make
Sublime silently fail to register commands. Run standalone, not under pytest,
because it installs fake `sublime` / `sublime_plugin` modules into sys.modules.

    python tools/check_import.py
"""

import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _stub_sublime():
    m = types.ModuleType("sublime")

    class Region:
        def __init__(self, a, b=None):
            self.a, self.b = a, b if b is not None else a

    class Settings(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

        def set(self, k, v):
            self[k] = v

        def add_on_change(self, *a, **k):
            pass

        def clear_on_change(self, *a, **k):
            pass

    class QuickPanelItem:
        def __init__(self, trigger, details="", annotation="", kind=None):
            self.trigger = trigger
            self.details = details
            self.annotation = annotation
            self.kind = kind

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
    m.status_message = lambda msg: None
    m.error_message = lambda msg: None
    m.message_dialog = lambda msg: None
    m.windows = lambda: []
    m.active_window = lambda: None
    m.run_command = lambda *a, **k: None
    m.version = lambda: "4169"
    m.platform = lambda: "windows"
    m.arch = lambda: "x64"
    m.expand_variables = lambda s, v: s
    m.find_resources = lambda pattern: []
    m.load_resource = lambda path: ""
    m.DRAW_NO_OUTLINE = 256
    m.DRAW_NO_FILL = 32
    m.DRAW_EMPTY = 1
    m.PERSISTENT = 16
    m.HIDDEN = 128
    m.LAYOUT_INLINE = 0
    m.KIND_ID_AMBIGUOUS = 0
    m.MONOSPACE_FONT = 1
    m.HOVER_TEXT = 1
    return m


def _stub_sublime_plugin():
    m = types.ModuleType("sublime_plugin")

    class _Base:
        def __init__(self, *a, **k):
            pass

    class WindowCommand(_Base):
        pass

    class TextCommand(_Base):
        pass

    class ApplicationCommand(_Base):
        pass

    class EventListener(_Base):
        pass

    class ViewEventListener(_Base):
        pass

    class TextChangeListener(_Base):
        pass

    m.WindowCommand = WindowCommand
    m.TextCommand = TextCommand
    m.ApplicationCommand = ApplicationCommand
    m.EventListener = EventListener
    m.ViewEventListener = ViewEventListener
    m.TextChangeListener = TextChangeListener
    return m


def main():
    sys.modules.setdefault("sublime", _stub_sublime())
    sys.modules.setdefault("sublime_plugin", _stub_sublime_plugin())

    try:
        from ai import ai_terminal
    except Exception as e:
        import traceback

        traceback.print_exc()
        print("\ncheck_import: FAILED to import ai.ai_terminal: %r" % (e,))
        return 1

    expected = [
        "AiTerminalOpenHereCommand",
        "AiTerminalOpenInEditorCommand",
        "AiTerminalSelectProfileCommand",
        "AiTerminalLauncherCommand",
        "AiTerminalRecentSessionsCommand",
        "AiTerminalRefreshUsageCommand",
        "AiTerminalSendStringCommand",
        "AiTerminalKeypressCommand",
        "AiTerminalRenderCommand",
        "AiTerminalNukeCommand",
        "AiTerminalTrackpadScrollCommand",
        "AiTerminalViewListener",
        "AiTerminalKeyInterceptor",
    ]
    missing = [n for n in expected if not hasattr(ai_terminal, n)]
    if missing:
        print("check_import: missing classes: %s" % ", ".join(missing))
        return 1

    # The periodic-usage interval is plugin-level code the pure tests cannot
    # reach, and getting it wrong means either no refresh at all or a loop that
    # hammers provider endpoints. Exercise the parsing here.
    import sublime as _sub

    cases = [
        ({}, 20 * 60 * 1000, "default"),
        ({"usage_refresh_minutes": 5}, 5 * 60 * 1000, "explicit"),
        ({"usage_refresh_minutes": 0}, 0, "disabled"),
        ({"usage_refresh_minutes": -3}, 0, "negative disables"),
        ({"usage_refresh_minutes": 0.1}, 60 * 1000, "clamped to 1 minute"),
        ({"usage_refresh_minutes": "nonsense"}, 20 * 60 * 1000, "bad value"),
    ]
    failures = []
    for values, want, label in cases:
        settings = _sub.Settings()
        settings.update(values)
        ai_terminal._settings = settings
        got = ai_terminal._usage_refresh_interval_ms()
        if got != want:
            failures.append("  %s: got %r, want %r" % (label, got, want))
    ai_terminal._settings = None
    if failures:
        print("check_import: usage refresh interval wrong:\n" + "\n".join(failures))
        return 1

    print("check_import: ai.ai_terminal imports cleanly; %d classes present; "
          "usage refresh interval OK" % len(expected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
