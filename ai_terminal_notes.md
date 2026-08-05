# ai_terminal Notes

Running technical notes on ai_terminal.py internals that aren't obvious from
the code alone. Newest entries at the top.

## 2026-08-05 — Hover-motion (xterm mode 1003) forwarding via OS polling

**Problem:** Textual TUIs (e.g. pybackup's TUI) enable xterm mode 1003
"any-event" mouse tracking to drive hover-highlight — they need a report for
every cell the cursor crosses, with no button held. Sublime's plugin API has
no such event. `EventListener.on_hover` looked like the candidate, but
confirmed empirically (monkey-patching `sublime_plugin.on_hover`, moving the
mouse continuously for 10s) it fires only ~4 times in 10s (every 2.4-5.1s) —
a debounced settle signal, not a continuous stream. Checked LSP's own hover
popup implementation (`LSP.sublime-package` → `plugin/hover.py`) for a
counter-example: it uses the exact same debounced `on_hover` entry point, and
relies on a *native* ST flag (`PopupFlags.HIDE_ON_MOUSE_MOVE_AWAY`) for
move-away dismissal — i.e. it never receives a continuous stream either. So
there's no ST plugin event that fits.

**Fix:** `plugin_host` is a real, unsandboxed Python process, so it can call
Windows APIs directly via `ctypes`, independent of ST's own event dispatch.
Added a self-rescheduling `sublime.set_timeout` loop (`_hover_poll_loop` /
`_hover_poll_tick`, ~line 4763+, next to the existing `_clamp_vp_loop`) that:

1. `_hover_st_hwnd()` — `GetForegroundWindow()` + `GetClassNameW` check for
   `"PX_WINDOW_CLASS"` (ST4's window class, confirmed live via `EnumWindows`).
   Skips the tick entirely if OS focus isn't on an ST window.
2. `sublime.active_window().active_view()` → resolve the `_Terminal`. Skip if
   not a terminal, `mouse_handling` isn't enabled for its profile, or the app
   hasn't requested mode 1003 (`term.screen.mouse_tracking < 1003`).
3. `GetCursorPos` (screen coords) → `ScreenToClient(hwnd, ...)`. Confirmed
   live this client-relative pixel coordinate is exactly the space
   `view.window_to_text()` expects — the same space ST's own mouse-command
   `event["x"]/["y"]` args use.
4. `view.window_to_text()` + `view.rowcol()` → `_view_point_to_cell()`
   (existing helper, shared with click routing) → 1-based PTY cell, or
   `None` if off-grid.
5. Only sends when the cell differs from `_hover_last_cell[view_id]`
   (dedup) — forwards `_encode_mouse(BTN_RELEASE_X10, col, row, press=True,
   motion=True, sgr=...)`, the standard xterm "motion, no button" SGR report
   (`\x1b[<35;col;rowM`). No protocol changes needed — `terminal/mouse.py`'s
   `encode_mouse` already supported this shape.

Poll rate: 33ms (~30Hz), cheap due to the early-exits above. Loop starts in
`plugin_loaded()`, "stops" in `plugin_unloaded()` — though note
`sublime.set_timeout` always returns `None` in this ST version (confirmed
via the API stub), so the `_hover_poll_token`/`_clamp_token`/`_poll_token`
variables are vestigial in this codebase; cancellation is a no-op and the
loops just keep self-rescheduling regardless. `_hover_last_cell` is cleared
per-view in `_Terminal.kill()` alongside `_MOUSE_LAST_CLICK`/`_last_mouse_cell`.

Added `import ctypes` at module top. `BTN_RELEASE_X10` had to be added to
**both** `from .terminal.mouse import (...)` blocks — this file duplicates
its `terminal.*` imports twice (top-level + a nested ImportError-fallback
block for tests/scripts using `ai.*` instead of relative imports) — easy to
patch only one and get an `ImportError` under the other invocation path.

**Known limitation:** only forwards hover for `window.active_view()` of the
OS-foreground ST window. A terminal in a background pane/window while
another view has focus won't get hover motion. Acceptable for the
hover-highlight use case — a user can only be pointing at what's actually
focused/foreground.

**Result:** confirmed working live against pybackup's Textual TUI ("works
fairly well, is quick" — remaining visual quirks in the highlight styling
are pybackup's own rendering, not ai_terminal's).

### Menu wiring fixed in the same pass

The pre-existing "PyBackup Textual TUI" menu item (`Main.sublime-menu`) and
command-palette entry (`Default.sublime-commands`) were wired to a separate
launcher, `launchers/pb_tui_launcher.py`, that opened the TUI via **Terminus**
— a plugin that isn't installed, so the command silently no-op'd (both the
primary and fallback code paths called `terminus_open`). Repointed both
entries to `ai_terminal_open_here` with `args: {"profile": "Pybackup Textual
TUI"}` — that profile already existed in `ai_terminal.sublime-settings` with
`mouse_handling: true, force_main_screen: false`. Removed
`launchers/pb_tui_launcher.py` and its import in `PluginLoader.py`.

### Deploy caveat

Mirroring an edited `PluginLoader.py` into the live `Packages\User` tree
triggers a **full Sublime Text restart** — unlike `ai_terminal.py`, which
reloads surgically via `sublime_plugin.reload_plugin("User.ai.ai_terminal")`
with no restart. `PluginLoader.py` is the top-level loader ST rescans
wholesale on any change. Warn before deploying a `PluginLoader.py` edit live,
especially mid-session.
