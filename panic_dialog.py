"""panic_dialog.py — Quote-and-Reply panel for Claude conversations.

Opens two side-by-side views:
  Left  — response (read-only), each paragraph has a [Quote] phantom button
  Right — reply (editable), [Send] phantom at bottom injects into Terminus

Commands:
  panic_open   — open the panel (reads last response from JSONL transcript)
"""

import glob
import json
import os
import urllib.parse

import sublime
import sublime_plugin

_AI_VIEW_SETTING = "ai_logger"
_PANIC_REPLY_FILE = os.path.join(os.path.expanduser("~"), ".claude", "panic_reply.txt")
_RESPONSE_VIEW = "Panic: Response"
_REPLY_VIEW = "Panic: Reply"
_QUOTE_KEY = "panic_quotes"
_SEND_KEY = "panic_send"

# Keep PhantomSets alive (GC'd if not referenced)
_phantom_sets = {}


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


# ── Terminus sender ───────────────────────────────────────────────────────────

def _send_to_terminus(window, text):
    # Write full reply to file; send short trigger command to terminal
    # (multi-line text can't be sent raw — terminal treats \n as Enter)
    os.makedirs(os.path.dirname(_PANIC_REPLY_FILE), exist_ok=True)
    with open(_PANIC_REPLY_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    for view in window.views():
        if view.settings().get(_AI_VIEW_SETTING):
            view.run_command("terminus_send_string", {"string": "read panic\n"})
            window.focus_view(view)
            return
    sublime.error_message("No Ai terminal found — open Claude Code first.")


# ── View lookup (no self dependency) ─────────────────────────────────────────

def _find_view(name):
    """Find a view by name across all windows. Returns (window, view) or (None, None)."""
    for w in sublime.windows():
        for v in w.views():
            if v.name() == name:
                return w, v
    return None, None


def _get_or_create_view(window, name, group):
    for v in window.views():
        if v.name() == name:
            window.set_view_index(v, group, 0)
            return v
    v = window.new_file()
    v.set_name(name)
    v.set_scratch(True)
    window.set_view_index(v, group, 0)
    return v


def _set_view_text(view, text):
    view.set_read_only(False)
    view.run_command("select_all")
    view.run_command("right_delete")
    view.run_command("append", {"characters": text})


# ── Phantom builders ──────────────────────────────────────────────────────────

def _quote_btn_html(href):
    return (
        '<a href="{}" style="background-color:#313244;color:#89b4fa;'
        'padding:1px 9px;text-decoration:none;border-radius:3px;'
        'font-size:11px;font-family:system-ui;">Quote ↓</a>'.format(href)
    )


def _send_btn_html():
    return (
        '<a href="send:" style="background-color:#89b4fa;color:#1e1e2e;'
        'padding:4px 18px;text-decoration:none;border-radius:4px;'
        'font-size:13px;font-weight:bold;font-family:system-ui;">Send ↵</a>'
        '&nbsp;&nbsp;'
        '<a href="cancel:" style="background-color:#313244;color:#cdd6f4;'
        'padding:4px 14px;text-decoration:none;border-radius:4px;'
        'font-size:13px;font-family:system-ui;">Cancel</a>'
    )


def _build_quote_phantoms(resp_view):
    content = resp_view.substr(sublime.Region(0, resp_view.size()))
    phantoms = []
    pos = 0
    for para in content.split("\n\n"):
        end = pos + len(para)
        if para.strip():
            href = "quote:" + urllib.parse.quote(para.strip(), safe="")
            ph = sublime.Phantom(
                sublime.Region(end),
                _quote_btn_html(href),
                sublime.LAYOUT_BLOCK,
                _on_quote,
            )
            phantoms.append(ph)
        pos = end + 2
    ps = sublime.PhantomSet(resp_view, _QUOTE_KEY)
    ps.update(phantoms)
    _phantom_sets[resp_view.id()] = ps


def _build_send_phantom(reply_view):
    ph = sublime.Phantom(
        sublime.Region(reply_view.size()),
        _send_btn_html(),
        sublime.LAYOUT_BLOCK,
        _on_send,
    )
    ps = sublime.PhantomSet(reply_view, _SEND_KEY)
    ps.update([ph])
    _phantom_sets[reply_view.id()] = ps


# ── Module-level callbacks (no self, no GC risk) ──────────────────────────────

def _on_quote(href):
    if not href.startswith("quote:"):
        return
    para = urllib.parse.unquote(href[6:])
    quoted = "\n".join("> " + l for l in para.split("\n")) + "\n\n"
    w, reply = _find_view(_REPLY_VIEW)
    if not reply:
        return
    reply.run_command("move_to", {"to": "eof"})
    reply.run_command("append", {"characters": quoted})
    w.focus_view(reply)
    reply.run_command("move_to", {"to": "eof"})
    _build_send_phantom(reply)


def _on_send(href):
    if href == "cancel:":
        _close_panic()
        return
    if href != "send:":
        return
    w, reply = _find_view(_REPLY_VIEW)
    if not reply:
        return
    text = reply.substr(sublime.Region(0, reply.size())).strip()
    if not text:
        sublime.status_message("Panic: reply is empty.")
        return
    _send_to_terminus(w, text)
    _close_panic()


def _close_panic():
    for name in (_RESPONSE_VIEW, _REPLY_VIEW):
        w, v = _find_view(name)
        if v:
            _phantom_sets.pop(v.id(), None)
            v.close()
    for w in sublime.windows():
        current = w.get_layout()
        if len(current.get("cols", [])) > 2:
            w.set_layout({
                "cols": [0.0, 1.0],
                "rows": [0.0, 1.0],
                "cells": [[0, 0, 1, 1]],
            })


# ── Command ───────────────────────────────────────────────────────────────────

class PanicOpenCommand(sublime_plugin.WindowCommand):
    """Open the Quote-and-Reply panel with Claude's last response."""

    def run(self, response_text=None):
        if response_text is None:
            response_text = _last_claude_response()
        if not response_text:
            sublime.error_message("No Claude response found in transcript.")
            return

        self.window.set_layout({
            "cols": [0.0, 0.52, 1.0],
            "rows": [0.0, 1.0],
            "cells": [[0, 0, 1, 1], [1, 0, 2, 1]],
        })

        resp = _get_or_create_view(self.window, _RESPONSE_VIEW, 0)
        _set_view_text(resp, response_text)
        resp.set_read_only(True)
        _build_quote_phantoms(resp)

        reply = _get_or_create_view(self.window, _REPLY_VIEW, 1)
        _set_view_text(reply, "")
        _build_send_phantom(reply)
        self.window.focus_view(reply)
