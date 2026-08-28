"""pybackup_panel.py -- native ST panel for pybackup, talking to a plain
stdio JSON daemon (backend_daemon.py). No Flask, no HTTP, no port, no
browser, no PTY, no ANSI, no gotui.exe.

Plain top-level plugin file directly in this SText package (formerly its own
package, symlinked in via STRepoInstall -- that project is retired). Kept as
one file rather than split per-command: PybackupInsertAtCommand,
PybackupOpenCommand, and PybackupPanelCloseListener all share the module-level
daemon process/panel registry (_panels, _daemon_proc, etc.) -- splitting them
apart would each get an independent copy of that state, breaking correctness
(e.g. the close listener would never see panels the open command registered),
not just repeating code. This is pybackup's UI, not a generic terminal
launcher concern.

Runs in its own ST window, split into two stacked rows/tabs: the log view
on top gets plain-text appends (real ST scrollback/select, no tricks), and
the UI view on the bottom holds a single minihtml phantom (toolbar +
table); every click is a real <a href> through on_navigate -- no
pixel/region click-routing of any kind. Keeping log and UI in separate
views means the UI phantom no longer has to coexist with growing log text
in the same buffer.
"""

import html
import json
import os
import shutil
import subprocess
import threading

import sublime
import sublime_plugin

_REPO_ROOT = r"C:\Users\donal\projects\pybackup"
_DAEMON_PY = os.path.join(_REPO_ROOT, "ui", "sublime_panel", "daemon", "backend_daemon.py")
_VIEW_SETTING = "pybackup_panel_view"
_PHANTOM_KEY = "pybackup_panel"
_RESP_MARKER = "@@PYBACKUP-RESP@@ "

_panels = {}  # ui_view_id -> _PanelState
_log_to_ui = {}  # log_view_id -> ui_view_id


class _PanelState:
    def __init__(self, log_view, ui_view):
        self.log_view = log_view
        self.ui_view = ui_view
        self.phantom_set = sublime.PhantomSet(ui_view, _PHANTOM_KEY)
        self.sel = {}  # row key -> bool
        self.status = None  # last "status" response payload
        self.alive = True
        self.inspect_key = None  # row key currently expanded, or None
        self.inspect_data = None  # cached debug_edge response for inspect_key
        self.predict = None  # {"keys": [...], "data": dict|None, "delete_keys": set()} or None
        # ui_view holds nothing but this one phantom, so the anchor never
        # has to move -- unlike the old single-view layout, log growth in
        # log_view can't push it around.
        self.anchor_pos = 0


# -- daemon process + stdio JSON-line transport -------------------------------
#
# Exactly one backend_daemon.py subprocess is shared by every panel view in
# this plugin_host process. Requests go out on its stdin as one JSON object
# per line; matching responses come back on its stdout, marker-prefixed so
# they can be told apart from the plain pblog() log lines that also land on
# that same stdout (pblog already prints every line it writes -- reusing
# that as the live log feed, instead of tailing a file over a network
# connection, is what let the whole SSE/log-rotation saga this session go
# away entirely).

_daemon_proc = None  # subprocess.Popen | None
_daemon_lock = threading.Lock()  # serializes stdin writes + id allocation
_next_id = 1
_pending = {}  # request id -> threading.Event (result stashed on the event)
_ready_event = threading.Event()


def _dispatch_log_line(text):
    for state in list(_panels.values()):
        sublime.set_timeout(lambda t=text, s=state: _append_log_line(s, t), 0)


def _daemon_stdout_reader(proc):
    for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            continue
        if line.startswith(_RESP_MARKER):
            try:
                resp = json.loads(line[len(_RESP_MARKER):])
            except Exception:
                continue
            if resp.get("id") is None:
                if resp.get("event") == "ready":
                    _ready_event.set()
                continue
            ev = _pending.get(resp["id"])
            if ev is not None:
                ev.result = resp
                ev.set()
        else:
            _dispatch_log_line(line)
    _ready_event.set()  # unblock any _start_backend() wait if the daemon died


def _daemon_stderr_reader(proc):
    for raw in proc.stderr:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line:
            _dispatch_log_line("[daemon stderr] " + line)


def _call(cmd, timeout=10, **params):
    global _next_id
    if _daemon_proc is None or _daemon_proc.poll() is not None:
        raise RuntimeError("pybackup backend is not running")
    with _daemon_lock:
        req_id = _next_id
        _next_id += 1
        ev = threading.Event()
        ev.result = None
        _pending[req_id] = ev
        payload = {"id": req_id, "cmd": cmd, **params}
        _daemon_proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        _daemon_proc.stdin.flush()
    try:
        if not ev.wait(timeout):
            raise TimeoutError(f"pybackup: {cmd} timed out")
        resp = ev.result
        if resp.get("error"):
            raise RuntimeError(resp["error"])
        return resp.get("data")
    finally:
        _pending.pop(req_id, None)


def _backend_reachable():
    return _daemon_proc is not None and _daemon_proc.poll() is None and _ready_event.is_set()


def _start_backend():
    global _daemon_proc
    if _backend_reachable():
        return
    _ready_event.clear()
    # sys.executable inside ST's plugin_host is plugin_host-3.8.exe, not a
    # usable Python interpreter -- always resolve a real "python" off PATH.
    python = shutil.which("python") or shutil.which("python3") or "python"
    # python.exe is a console-subsystem binary -- without CREATE_NO_WINDOW,
    # Windows pops a visible console for it that stays on top and never
    # goes away since nothing ever attaches/hides it.
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    _daemon_proc = subprocess.Popen(
        [python, "-u", _DAEMON_PY],
        cwd=_REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    threading.Thread(target=_daemon_stdout_reader, args=(_daemon_proc,), daemon=True).start()
    threading.Thread(target=_daemon_stderr_reader, args=(_daemon_proc,), daemon=True).start()
    if not _ready_event.wait(20):
        raise RuntimeError("pybackup backend did not respond within 20s")
    if _daemon_proc.poll() is not None:
        raise RuntimeError("pybackup backend exited during startup")


def _stop_backend():
    global _daemon_proc
    proc = _daemon_proc
    _daemon_proc = None
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# -- rendering ------------------------------------------------------------

_TOOLBAR = [
    ("scan", "\u21bb Scan"),
    ("run", "\u25b6 Run Selected"),
    ("allnone", "\u2611 All/None"),
    ("quit", "\u2715 Quit"),
]

_STYLE = """
<style>
  div.toolbar { padding: 4px 0; }
  a.btn {
    display: inline-block; margin-right: 8px; padding: 2px 8px;
    text-decoration: none; border-radius: 3px;
    background-color: color(var(--background) blend(var(--foreground) 90%));
    color: var(--foreground);
  }
  a.btn.run { background-color: color(#2ecc71 blend(var(--background) 70%)); }
  a.btn.quit { background-color: color(#e74c3c blend(var(--background) 70%)); }
  div.row {
    font-family: monospace; white-space: pre; padding: 1px 0;
  }
  a.chk { text-decoration: none; color: var(--foreground); font-family: monospace; white-space: pre; }
  span.notice { color: color(var(--foreground) alpha(0.6)); }
</style>
"""

# minihtml has no <table>/width -- align columns with monospace padding
# baked into the text itself (white-space: pre keeps the spaces literal).
_COL_DESC = 46
_COL_TYPE = 12
_COL_COUNTS = 18


def _fmt_file(f):
    sz = f.get("sz")
    sz_str = f"({sz/1024:.1f} KB)" if isinstance(sz, (int, float)) else ""
    return f'    {html.escape(f.get("nm",""))}  {sz_str}'


def _inspect_html(state, key):
    data = state.inspect_data
    if data is None:
        return '<div class="row" style="opacity:0.6">    Loading\u2026</div>'
    if data.get("error"):
        return f'<div class="row" style="color:#e74c3c">    Error: {html.escape(str(data["error"]))}</div>'
    lines = []
    f2c = data.get("f2c") or []
    f2d = data.get("f2d") or []
    f2t = data.get("f2t") or []
    if f2c:
        lines.append(f'<div class="row" style="color:#3498db">  COPY ({len(f2c)})</div>')
        lines += [f'<div class="row">{_fmt_file(f)}</div>' for f in f2c]
    if f2d:
        lines.append(f'<div class="row" style="color:#e74c3c">  DELETE \u2014 orphans in dest ({len(f2d)})</div>')
        lines += [f'<div class="row">{_fmt_file(f)}</div>' for f in f2d]
    if f2t:
        lines.append(f'<div class="row" style="color:#f1c40f">  TOUCH ({len(f2t)})</div>')
        lines += [f'<div class="row">{_fmt_file(t.get("src", {}))}</div>' for t in f2t]
    if not lines:
        lines.append('<div class="row" style="opacity:0.6">    No pending files.</div>')
    return "".join(lines)


def _row_html(state, row, checked):
    key = row["key"]
    # Only the mark itself is clickable, not the surrounding brackets -- the
    # brackets are just visual framing. A literal space doesn't reliably
    # hit-test as clickable in minihtml, so use &nbsp; (a real glyph) for
    # the unchecked mark.
    mark = "✓" if checked else "&nbsp;"
    box = f'[<a class="chk" href="toggle:{html.escape(key)}">{mark}</a>]'
    risk = row.get("risk") or ""
    # Plain bullet (U+25CF) for every risk level, colored via CSS -- the
    # red/yellow circle *emoji* (U+1F534/U+1F7E1) render through an emoji
    # font at double-cell width regardless of font-family: monospace on the
    # row, which throws off every ljust()-padded column after it in views
    # that resolve those codepoints to a color-emoji font.
    dot_color = {"high": "#e74c3c", "medium": "#f1c40f"}.get(risk)
    dot = f'<span style="color:{dot_color}">\u25cf</span>' if dot_color else "\u25cf"
    git_state = row.get("git_state") or ""
    f2c = row.get("f2c")
    f2d = row.get("f2d")
    f2t = row.get("f2t")
    counts = " ".join(
        s
        for s in (
            f"copy {f2c}" if f2c else "",
            f"del {f2d}" if f2d else "",
            f"touch {f2t}" if f2t else "",
        )
        if s
    )
    desc = f'{row.get("si","")} \u2192 {row.get("di","")}'
    desc = desc[: _COL_DESC - 1] if len(desc) >= _COL_DESC else desc.ljust(_COL_DESC)
    rtype = row.get("type", "").ljust(_COL_TYPE)
    counts_padded = counts.ljust(_COL_COUNTS)
    text = f" {dot} {html.escape(desc)}{html.escape(rtype)}{html.escape(counts_padded)}{html.escape(str(git_state))}"
    out = (
        '<div class="row">'
        f'{box}{text}'
        f' <a class="chk" href="inspect:{html.escape(key)}" title="Inspect files">\U0001F50D</a>'
        f' <a class="chk" href="runone:{html.escape(key)}" title="Preview and run this row only">\u25b6</a>'
        "</div>"
    )
    if state.inspect_key == key:
        out += _inspect_html(state, key)
    return out


def _visible(op):
    """Mirrors ui/app.py's renderOps() JS filter -- hide rows with nothing
    pending (no local change, no diff counts, no outstanding git op)."""
    f2c = op.get("f2c") or 0
    f2d = op.get("f2d") or 0
    f2t = op.get("f2t") or 0
    git_state = op.get("git_state")
    return bool(
        op.get("src_changed")
        or (op.get("scanned") and (f2c + f2d + f2t) > 0)
        or (op.get("type") == "GitOps" and f2c > 0)
        or (git_state == 2 and op.get("dst_remote"))
        or (git_state == 3 and not op.get("dst_remote"))
        or (git_state == 4)
    )


def _default_selected(op):
    """Mirrors app.py's `checked` default: src_changed or real pending work."""
    f2c = op.get("f2c") or 0
    f2d = op.get("f2d") or 0
    f2t = op.get("f2t") or 0
    git_state = op.get("git_state")
    has_active_work = (
        f2c > 0
        or f2t > 0
        or (op.get("type") == "Mkzip" and f2d > 0)
        or (git_state == 2 and op.get("dst_remote"))
        or (git_state == 3 and not op.get("dst_remote"))
    )
    return bool(op.get("src_changed") or has_active_work)


_RISK_BANNER = {
    "ok": ("#2ecc71", "All clear — no destructive actions detected."),
    "warn": ("#f1c40f", "Caution — review warnings before proceeding."),
    "danger": ("#e74c3c", "DANGER — potentially destructive. Read carefully."),
}


def _predict_html(state):
    p = state.predict
    if p["data"] is None:
        return '<div class="row" style="opacity:0.6">Analyzing…</div>'
    d = p["data"]
    if d.get("error"):
        return (
            f'<div class="row" style="color:#e74c3c">Prediction error: {html.escape(str(d["error"]))}</div>'
            '<div class="toolbar"><a class="btn" href="predcancel">Cancel</a></div>'
        )
    color, label = _RISK_BANNER.get(d.get("risk"), _RISK_BANNER["warn"])
    out = [f'<div class="row" style="color:{color}">{html.escape(label)}</div><div class="row"></div>']
    edges = d.get("edges") or {}
    if not edges:
        out.append('<div class="row" style="opacity:0.6">No scanned edges selected.</div>')
    for key, ed in edges.items():
        di, _, si = key.partition("|")
        color, _ = _RISK_BANNER.get(ed.get("risk"), _RISK_BANNER["ok"])
        out.append(
            f'<div class="row" style="color:{color}">{si} → {di}'
            f'  copy {ed.get("f2c",0)}  del {ed.get("f2d",0)}</div>'
        )
        for w in ed.get("warnings") or []:
            out.append(f'<div class="row">    {html.escape(w)}</div>')
        if ed.get("can_delete"):
            checked = key in p["delete_keys"]
            mark = "✓" if checked else "&nbsp;"
            box = f'[<a class="chk" href="preddel:{html.escape(key)}">{mark}</a>]'
            out.append(
                f'<div class="row">{box}'
                f'  Enable deletion of {ed.get("f2d",0)} orphaned file(s) from {di}</div>'
            )
    out.append('<div class="row"></div>')
    confirm_label = "⚠ Run Anyway" if d.get("risk") == "danger" else "Confirm Run"
    out.append(
        '<div class="toolbar">'
        f'<a class="btn run" href="predconfirm">{confirm_label}</a>'
        '<a class="btn" href="predcancel">Cancel</a>'
        "</div>"
    )
    return "".join(out)


def _panel_html(state):
    if state.predict is not None:
        return _STYLE + _predict_html(state)
    if state.status is None:
        return (
            _STYLE
            + '<div class="toolbar"><span class="notice">pybackup &mdash; starting backend&hellip;</span></div>'
        )

    status = state.status or {}
    all_rows = status.get("ops") or []
    rows = [r for r in all_rows if _visible(r)]
    busy = bool(status.get("scanning") or status.get("running"))
    for row in rows:
        if row["key"] not in state.sel:
            state.sel[row["key"]] = _default_selected(row)
    state_word = "scanning" if status.get("scanning") else (
        "running" if status.get("running") else "idle"
    )
    buttons = []
    for action, label in _TOOLBAR:
        cls = "btn"
        if action == "run":
            cls += " run"
        if action == "quit":
            cls += " quit"
        disabled = busy and action in ("scan", "run")
        if disabled:
            buttons.append(f'<span class="btn" style="opacity:0.4">{label}</span>')
        else:
            buttons.append(f'<a class="{cls}" href="action:{action}">{label}</a>')
    if rows:
        row_html = "".join(_row_html(state, r, state.sel.get(r["key"], False)) for r in rows)
    else:
        row_html = '<div class="row" style="opacity:0.6">All clean ✓</div>'
    return (
        _STYLE
        + '<div class="toolbar">'
        + f'<span class="notice">pybackup &mdash; {state_word}</span>&nbsp;&nbsp;'
        + "".join(buttons)
        + "</div>"
        + row_html
    )


def _refresh_phantom(state, scroll=False):
    view = state.ui_view
    if not view.is_valid():
        return
    region = sublime.Region(state.anchor_pos, state.anchor_pos)
    phantom = sublime.Phantom(
        region, _panel_html(state), sublime.LAYOUT_BLOCK, on_navigate=lambda href, s=state: _on_navigate(s, href)
    )
    state.phantom_set.update([phantom])
    # Only force the viewport back to the top on an actual screen
    # transition (entering/leaving predict mode). Routine content updates
    # (poll ticks, checkbox toggles, inspect expansion) must leave the
    # user's scroll position alone -- forcing it every refresh made
    # buttons below the fold (e.g. "Run Anyway") unreachable, since a poll
    # tick landing mid-click would snap the view back to the top first.
    if scroll:
        view.show(state.anchor_pos, animate=False)


# -- navigation / actions ---------------------------------------------------


def _on_navigate(state, href):
    kind, _, arg = href.partition(":")
    if kind == "toggle":
        state.sel[arg] = not state.sel.get(arg, False)
        _refresh_phantom(state)
    elif kind == "action":
        if arg == "allnone":
            any_off = any(not v for v in state.sel.values())
            for k in list(state.sel.keys()):
                state.sel[k] = any_off
            _refresh_phantom(state)
        elif arg == "quit":
            window = state.ui_view.window()
            if window is not None:
                window.run_command("close_window")
        else:
            threading.Thread(target=_run_action, args=(state, arg), daemon=True).start()
    elif kind == "inspect":
        threading.Thread(target=_toggle_inspect, args=(state, arg), daemon=True).start()
    elif kind == "runone":
        threading.Thread(target=_start_predict, args=(state, [arg]), daemon=True).start()
    elif kind == "preddel":
        if state.predict is None:
            return
        dk = state.predict["delete_keys"]
        if arg in dk:
            dk.discard(arg)
        else:
            dk.add(arg)
        _refresh_phantom(state)
    elif kind == "predconfirm":
        threading.Thread(target=_confirm_predict, args=(state,), daemon=True).start()
    elif kind == "predcancel":
        state.predict = None
        _refresh_phantom(state, scroll=True)


def _toggle_inspect(state, key):
    if state.inspect_key == key:
        state.inspect_key = None
        state.inspect_data = None
        sublime.set_timeout(lambda: _refresh_phantom(state), 0)
        return
    state.inspect_key = key
    state.inspect_data = None
    sublime.set_timeout(lambda: _refresh_phantom(state), 0)  # shows "Loading…" immediately
    di, _, si = key.partition("|")
    try:
        data = _call("debug_edge", di=di, si=si)
    except Exception as e:
        data = {"error": str(e)}
    if state.inspect_key == key:  # user may have toggled elsewhere meanwhile
        state.inspect_data = data
        sublime.set_timeout(lambda: _refresh_phantom(state), 0)


def _start_predict(state, keys):
    if not keys:
        sublime.status_message("pybackup: select at least one row")
        return
    state.predict = {"keys": keys, "data": None, "delete_keys": set()}
    sublime.set_timeout(lambda: _refresh_phantom(state, scroll=True), 0)  # shows "Analyzing…" immediately
    try:
        data = _call("predict", keys=keys, delete_keys=[])
    except Exception as e:
        data = {"risk": "warn", "edges": {}, "error": str(e)}
    if state.predict is not None and state.predict["keys"] == keys:
        state.predict["data"] = data
        sublime.set_timeout(lambda: _refresh_phantom(state), 0)


def _confirm_predict(state):
    p = state.predict
    if p is None:
        return
    try:
        _call("run", keys=list(p["keys"]), delete_keys=list(p["delete_keys"]))
    except Exception as e:
        sublime.status_message(f"pybackup: run failed ({e})")
    state.predict = None
    sublime.set_timeout(lambda: _refresh_phantom(state, scroll=True), 0)


def _run_action(state, action):
    try:
        if action == "scan":
            _call("scan")
        elif action == "run":
            keys = [k for k, v in state.sel.items() if v]
            _start_predict(state, keys)
    except Exception as e:
        sublime.status_message(f"pybackup: {action} failed ({e})")


# -- polling -------------------------------------------------------------


def _poll_loop(state):
    if not state.alive or not state.ui_view.is_valid():
        return
    try:
        status = _call("status", timeout=3)
        state.status = status
        sublime.set_timeout(lambda: _refresh_phantom(state), 0)
    except Exception:
        pass
    interval_ms = 2000 if (state.status or {}).get("scanning") or (state.status or {}).get("running") else 4000
    sublime.set_timeout(lambda: _poll_thread_tick(state), interval_ms)


def _poll_thread_tick(state):
    if not state.alive:
        return
    threading.Thread(target=_poll_loop, args=(state,), daemon=True).start()


def _start_backend_and_poll(state):
    """Runs off the main thread so window creation isn't blocked on the
    daemon subprocess starting up (which can take several seconds)."""
    try:
        _start_backend()
    except Exception as e:
        _dispatch_log_line(f"pybackup: failed to start backend: {e}")
        return
    _poll_loop(state)


def _append_log_line(state, line):
    view = state.log_view
    if not view.is_valid():
        return
    text = line + "\n"
    pos = view.size()
    view.set_read_only(False)
    view.run_command("pybackup_insert_at", {"pos": pos, "text": text})
    view.set_read_only(True)
    view.show(view.size(), animate=False)


# -- commands -----------------------------------------------------------------


class PybackupInsertAtCommand(sublime_plugin.TextCommand):
    """Insert text at a fixed buffer position, bypassing read-only."""

    def run(self, edit, pos, text):
        self.view.insert(edit, pos, text)


class PybackupOpenCommand(sublime_plugin.WindowCommand):
    """Open (or focus) the pybackup panel. Command palette: "Pybackup: Open"."""

    def run(self):
        for w in sublime.windows():
            for v in w.views():
                if v.settings().get(_VIEW_SETTING) == "ui":
                    w.focus_view(v)
                    return
        sublime.run_command("new_window")
        window = sublime.active_window()
        # Two stacked rows, one tab group each: log on top, UI on bottom.
        window.set_layout({
            "cols": [0.0, 1.0],
            "rows": [0.0, 0.5, 1.0],
            "cells": [[0, 0, 1, 1], [0, 1, 1, 2]],
        })

        log_view = window.new_file()
        log_view.set_name("Pybackup Log")
        log_view.set_scratch(True)
        log_view.settings().set(_VIEW_SETTING, "log")
        log_view.set_read_only(True)
        window.set_view_index(log_view, 0, 0)

        ui_view = window.new_file()
        ui_view.set_name("Pybackup")
        ui_view.set_scratch(True)
        ui_view.settings().set(_VIEW_SETTING, "ui")
        ui_view.settings().set("gutter", False)
        ui_view.set_read_only(True)
        window.set_view_index(ui_view, 1, 0)

        window.focus_view(ui_view)

        state = _PanelState(log_view, ui_view)
        _panels[ui_view.id()] = state
        _log_to_ui[log_view.id()] = ui_view.id()
        _refresh_phantom(state, scroll=True)  # shows "starting backend…" immediately
        threading.Thread(target=_start_backend_and_poll, args=(state,), daemon=True).start()


class PybackupPanelCloseListener(sublime_plugin.EventListener):
    def on_close(self, view):
        vid = view.id()
        state = _panels.pop(vid, None)
        if state is not None:
            _log_to_ui.pop(state.log_view.id(), None)
        else:
            ui_id = _log_to_ui.pop(vid, None)
            if ui_id is None:
                return
            state = _panels.pop(ui_id, None)
            if state is None:
                return
        state.alive = False
        if not _panels:
            threading.Thread(target=_stop_backend, daemon=True).start()


def plugin_unloaded():
    """ST calls this right before reloading/unloading this module (e.g. on
    every save while developing the plugin). Without it, the daemon process
    and poll-loop threads started by the *old* module instance would keep
    running orphaned -- invisible to the new module's _panels/_daemon_proc,
    so nothing could ever signal them to stop short of restarting ST."""
    for state in list(_panels.values()):
        state.alive = False
    _panels.clear()
    _log_to_ui.clear()
    threading.Thread(target=_stop_backend, daemon=True).start()
