"""panic_dialog.py — Quote-and-Reply panel for Claude conversations.

Opens two tabs in the current group:
  Panic: Response — read-only, each paragraph has a [Quote ↓] phantom button
  Panic: Reply    — editable; Ctrl+Enter sends, Escape cancels

Commands:
  panic_open   — open panel (reads last JSONL response if no arg given)
  panic_send   — send reply (bound to Ctrl+Enter in the reply view)
  panic_cancel — cancel / close both views
"""

import glob
import json
import os
import urllib.parse

import sublime
import sublime_plugin

_AI_VIEW_SETTING = "ai_logger"
_PANIC_VIEW_SETTING = "panic_reply_view"
_RESPONSE_VIEW = "Panic: Response"
_REPLY_VIEW = "Panic: Reply — Ctrl+Enter to Send"
_QUOTE_KEY = "panic_quotes"
_BTN_KEY = "panic_send"

_phantom_sets = {}
_saved_layout = {}  # layout saved before panic opens, restored on close

_PANIC_LAYOUT = {
    "cols": [0.0, 0.55, 1.0],
    "rows": [0.0, 0.56, 1.0],
    "cells": [[0, 0, 1, 1], [1, 0, 2, 1], [0, 1, 1, 2], [1, 1, 2, 2]],
}
# Groups: 0=upper-left (Ai), 1=upper-right (files), 2=lower-left (Response), 3=lower-right (Reply)


# ── JSONL reader ──────────────────────────────────────────────────────────────


def _last_claude_response():
    pattern = os.path.expanduser("~/.claude/projects/**/*.jsonl")
    files = glob.glob(pattern, recursive=True)
    if not files:
        return None
    latest = max(files, key=os.path.getmtime)
    last_text = None
    try:
        with open(latest, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message", {})
                if msg.get("role") == "assistant":
                    for block in msg.get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            last_text = block["text"]
    except OSError:
        return None
    return last_text


# ── Helpers ───────────────────────────────────────────────────────────────────


def _find_view(name):
    for w in sublime.windows():
        for v in w.views():
            if v.name() == name:
                return w, v
    return None, None


def _get_or_create_view(window, name):
    for v in window.views():
        if v.name() == name:
            return v
    v = window.new_file()
    v.set_name(name)
    v.set_scratch(True)
    return v


def _set_view_text(view, text):
    view.set_read_only(False)
    view.run_command("select_all")
    view.run_command("right_delete")
    view.run_command("append", {"characters": text})


def _close_panic():
    target_window = None
    for name in (_RESPONSE_VIEW, _REPLY_VIEW):
        w, v = _find_view(name)
        if v:
            _phantom_sets.pop(v.id(), None)
            _phantom_sets.pop(str(v.id()) + "_btn", None)
            if w:
                target_window = w
            v.close()
    if _saved_layout and target_window:
        target_window.set_layout(_saved_layout.get("layout", _PANIC_LAYOUT))
        _saved_layout.clear()


# ── Phantom buttons ───────────────────────────────────────────────────────────
# Amber (#e6b450) editorial palette — annotation-tool aesthetic.
# Send/Cancel live at the END of the read-only Response view: stable, never moves.


def _quote_btn(href):
    return (
        '<a href="{}" style="background-color:#2d2a20;color:#e6b450;'
        "padding:2px 10px;text-decoration:none;border-radius:2px;"
        'font-size:11px;font-family:system-ui;letter-spacing:.03em;">❝ quote</a>'.format(
            href
        )
    )


def _send_area_html():
    rule = (
        '<span style="color:#3a3630;font-family:monospace;font-size:10px;">'
        + ("─" * 48)
        + "</span>"
    )
    send = (
        '<a href="send:" style="background-color:#e6b450;color:#111;'
        "padding:5px 22px;text-decoration:none;border-radius:3px;"
        'font-size:13px;font-weight:bold;font-family:system-ui;">Send reply</a>'
    )
    hint = (
        "&nbsp;&nbsp;&nbsp;"
        '<span style="color:#666;font-size:11px;font-family:system-ui;">'
        "empty = close</span>"
    )
    return rule + "<br>" + send + hint


def _build_action_buttons(resp_view):
    end = resp_view.size()
    ps = sublime.PhantomSet(resp_view, _BTN_KEY)
    ps.update(
        [
            sublime.Phantom(
                sublime.Region(end), _send_area_html(), sublime.LAYOUT_BLOCK, _on_action
            )
        ]
    )
    _phantom_sets[str(resp_view.id()) + "_btn"] = ps


def _on_action(href):
    if href == "send:":
        w, _ = _find_view(_REPLY_VIEW)
        if w:
            _do_send(w)
    elif href == "cancel:":
        _close_panic()


def _build_quote_phantoms(resp_view):
    content = resp_view.substr(sublime.Region(0, resp_view.size()))
    phantoms = []
    pos = 0
    for para in content.split("\n\n"):
        end = pos + len(para)
        if para.strip():
            href = "quote:" + urllib.parse.quote(para.strip(), safe="")
            phantoms.append(
                sublime.Phantom(
                    sublime.Region(end),
                    _quote_btn(href),
                    sublime.LAYOUT_BLOCK,
                    _on_quote,
                )
            )
        pos = end + 2
    ps = sublime.PhantomSet(resp_view, _QUOTE_KEY)
    ps.update(phantoms)
    _phantom_sets[resp_view.id()] = ps


def _on_quote(href):
    if not href.startswith("quote:"):
        return
    para = urllib.parse.unquote(href[6:])
    quoted = "\n".join("> " + l for l in para.split("\n")) + "\n\n"
    _, reply = _find_view(_REPLY_VIEW)
    if not reply:
        return
    reply.run_command("move_to", {"to": "eof"})
    reply.run_command("append", {"characters": quoted})
    # stay in Response view so user can keep quoting without switching back


# ── Send / Cancel ─────────────────────────────────────────────────────────────


def _do_send(window):
    w, reply = _find_view(_REPLY_VIEW)
    if not reply:
        return
    text = reply.substr(sublime.Region(0, reply.size())).strip()
    if not text:
        _close_panic()
        return
    # Send "read panic" to the Ai terminal — Claude reads the Reply tab and handles cleanup
    try:
        import sys as _sys

        _Terminal = _sys.modules["Terminus.terminus.terminal"].Terminal
        for _w in sublime.windows():
            for _v in _w.views():
                if _v.settings().get(_AI_VIEW_SETTING):
                    _t = _Terminal.from_id(_v.id())
                    if _t:
                        _t.send_string("read panic\n")
                    break
    except Exception as _e:
        sublime.status_message(f"Panic send error: {_e}")


# ── Commands ──────────────────────────────────────────────────────────────────


class PanicOpenCommand(sublime_plugin.WindowCommand):
    """Open the Quote-and-Reply panel."""

    def run(self, response_text=None):
        if response_text is None:
            response_text = _last_claude_response()
        if not (response_text or "").strip():
            response_text = "(no text response — type your reply below)"
        if not response_text.endswith("\n"):
            response_text += "\n"

        # Save current layout to restore on close
        _saved_layout["layout"] = self.window.get_layout()

        # Apply 2x2 grid: Ai=upper-left, files=upper-right, Response=lower-left, Reply=lower-right
        self.window.set_layout(_PANIC_LAYOUT)

        # Move Ai terminal to group 0 (upper-left)
        for v in self.window.views():
            if v.settings().get(_AI_VIEW_SETTING):
                self.window.set_view_index(v, 0, 0)
                break

        resp = _get_or_create_view(self.window, _RESPONSE_VIEW)
        _set_view_text(resp, response_text)
        resp.set_read_only(True)
        self.window.set_view_index(resp, 2, 0)

        reply = _get_or_create_view(self.window, _REPLY_VIEW)
        _set_view_text(reply, "")
        reply.settings().set(_PANIC_VIEW_SETTING, True)
        self.window.set_view_index(reply, 3, 0)
        self.window.focus_view(reply)

        def _build(_r=resp):
            _build_quote_phantoms(_r)
            _build_action_buttons(_r)

        sublime.set_timeout(_build, 100)

        sublime.status_message("Panic: Ctrl+Enter — Send  |  Escape — Cancel")


class PanicSendCommand(sublime_plugin.WindowCommand):
    """Send the panic reply (Ctrl+Enter in the reply view)."""

    def run(self):
        _do_send(self.window)

    def is_enabled(self):
        active = self.window.active_view()
        return active is not None and active.name() == _REPLY_VIEW


class PanicCancelCommand(sublime_plugin.WindowCommand):
    """Cancel the panic dialog."""

    def run(self):
        _close_panic()


class PanicRefreshCommand(sublime_plugin.WindowCommand):
    """Refresh the Response tab. Pass response_text directly, or fall back to JSONL."""

    def run(self, response_text=None):
        text = response_text or _last_claude_response()
        if not text:
            sublime.status_message("Panic: no response found")
            return
        if not text.endswith("\n"):
            text += "\n"
        _, resp = _find_view(_RESPONSE_VIEW)
        if not resp:
            sublime.status_message("Panic: Response tab not open")
            return
        _set_view_text(resp, text)
        resp.set_read_only(True)

        def _build(_r=resp):
            _build_quote_phantoms(_r)
            _build_action_buttons(_r)

        sublime.set_timeout(_build, 100)


def plugin_loaded():
    def _restore():
        _, resp = _find_view(_RESPONSE_VIEW)
        if resp:
            _build_quote_phantoms(resp)
            _build_action_buttons(resp)

    sublime.set_timeout(_restore, 200)
