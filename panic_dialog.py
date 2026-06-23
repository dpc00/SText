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
_PANIC_REPLY_FILE = os.path.join(os.path.expanduser("~"), ".claude", "panic_reply.txt")
_PANIC_VIEW_SETTING = "panic_reply_view"
_RESPONSE_VIEW = "Panic: Response"
_REPLY_VIEW = "Panic: Reply"
_QUOTE_KEY = "panic_quotes"

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
    for name in (_RESPONSE_VIEW, _REPLY_VIEW):
        w, v = _find_view(name)
        if v:
            _phantom_sets.pop(v.id(), None)
            v.close()


# ── Quote phantoms ────────────────────────────────────────────────────────────

def _quote_btn(href):
    return (
        '<a href="{}" style="background-color:#313244;color:#89b4fa;'
        'padding:1px 9px;text-decoration:none;border-radius:3px;'
        'font-size:11px;font-family:system-ui;">Quote ↓</a>'.format(href)
    )


def _build_quote_phantoms(resp_view):
    content = resp_view.substr(sublime.Region(0, resp_view.size()))
    phantoms = []
    pos = 0
    for para in content.split("\n\n"):
        end = pos + len(para)
        if para.strip():
            href = "quote:" + urllib.parse.quote(para.strip(), safe="")
            phantoms.append(sublime.Phantom(
                sublime.Region(end),
                _quote_btn(href),
                sublime.LAYOUT_BLOCK,
                _on_quote,
            ))
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
        sublime.status_message("Panic: reply is empty")
        return
    os.makedirs(os.path.dirname(_PANIC_REPLY_FILE), exist_ok=True)
    with open(_PANIC_REPLY_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    for view in w.views():
        if view.settings().get(_AI_VIEW_SETTING):
            view.run_command("terminus_send_string", {"string": "read panic\n"})
            w.focus_view(view)
            break
    else:
        sublime.error_message("No Ai terminal found — open Claude Code first.")
        return
    _close_panic()


# ── Commands ──────────────────────────────────────────────────────────────────

class PanicOpenCommand(sublime_plugin.WindowCommand):
    """Open the Quote-and-Reply panel."""

    def run(self, response_text=None):
        if response_text is None:
            response_text = _last_claude_response()
        if not response_text:
            sublime.error_message("No Claude response found in transcript.")
            return

        resp = _get_or_create_view(self.window, _RESPONSE_VIEW)
        _set_view_text(resp, response_text)
        resp.set_read_only(True)
        _build_quote_phantoms(resp)
        self.window.focus_view(resp)

        reply = _get_or_create_view(self.window, _REPLY_VIEW)
        _set_view_text(reply, "")
        reply.settings().set(_PANIC_VIEW_SETTING, True)
        self.window.focus_view(reply)

        sublime.status_message("Panic: Ctrl+Enter — Send  |  Escape — Cancel")


class PanicSendCommand(sublime_plugin.WindowCommand):
    """Send the panic reply (Ctrl+Enter in the reply view)."""

    def run(self):
        _do_send(self.window)

    def is_enabled(self):
        _, v = _find_view(_REPLY_VIEW)
        return v is not None


class PanicCancelCommand(sublime_plugin.WindowCommand):
    """Cancel the panic dialog."""

    def run(self):
        _close_panic()
