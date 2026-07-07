"""ai_hub.py — AI Hub dashboard panel for Sublime Text.

Opens with Ctrl+Alt+I. Shows inbox, status, and quick actions.
"""

import os
import re
import textwrap
import sublime
import sublime_plugin
from pathlib import Path

INBOX_PATH = Path(os.path.expanduser("~")) / "ideas_inbox.md"

# Catppuccin Mocha palette
BG      = "#1e1e2e"
BG2     = "#181825"
SURFACE = "#313244"
OVERLAY = "#6c7086"
TEXT    = "#cdd6f4"
SUBTEXT = "#a6adc8"
ACCENT  = "#cba6f7"   # mauve
BLUE    = "#89b4fa"
GREEN   = "#a6e3a1"
RED     = "#f38ba8"
YELLOW  = "#f9e2af"
PEACH   = "#fab387"


# ── parser ──────────────────────────────────────────────────────────────────

def _parse_inbox():
    if not INBOX_PATH.exists():
        return {}
    raw = INBOX_PATH.read_text(encoding="utf-8", errors="replace")
    sections = {}
    current = None
    for lineno, line in enumerate(raw.splitlines(), 1):
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            m = re.match(r"\s*- \[([ xX])\] (?:\[([^\]]*)\] )?(.+)", line)
            if m:
                sections[current].append({
                    "done":  m.group(1).lower() == "x",
                    "date":  (m.group(2) or "")[:10],
                    "text":  m.group(3).strip(),
                    "lineno": lineno,
                })
    return sections


# ── HTML builder ─────────────────────────────────────────────────────────────

def _e(s):
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


CSS = f"""
body {{
    background-color:{BG}; color:{TEXT};
    font-family:"Cascadia Code","Fira Code",Consolas,monospace;
    font-size:12px; margin:0; padding:14px 18px 24px;
}}
h1 {{ color:{ACCENT}; font-size:17px; margin:0 0 2px; padding:0; }}
.sub {{ color:{SUBTEXT}; font-size:10px; margin:0 0 14px; }}
h2 {{
    color:{BLUE}; font-size:11px; font-weight:bold;
    margin:16px 0 5px; padding:3px 8px;
    background-color:{SURFACE}; border-radius:3px;
    text-transform:uppercase; letter-spacing:0.5px;
}}
.item {{
    padding:3px 0 3px 10px;
    margin:2px 0;
    border-left:2px solid {SURFACE};
    color:{TEXT};
}}
.urgent {{
    border-left:3px solid {RED};
    background-color:{BG2};
    padding:4px 8px; border-radius:0 3px 3px 0;
    color:{RED};
}}
.done {{ color:{OVERLAY}; text-decoration:line-through; border-left:2px solid {BG2}; }}
.date {{ color:{OVERLAY}; font-size:10px; }}
a {{ color:{BLUE}; text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
.badge {{
    font-size:9px; font-weight:bold;
    padding:1px 5px; border-radius:2px; margin-right:5px;
}}
.b-bug   {{ background-color:{RED};    color:{BG}; }}
.b-ui    {{ background-color:{BLUE};   color:{BG}; }}
.b-setup {{ background-color:{YELLOW}; color:{BG}; }}
.b-mcp   {{ background-color:{ACCENT}; color:{BG}; }}
.b-note  {{ background-color:{OVERLAY};color:{BG}; }}
.actions {{
    margin-top:18px; padding:8px 10px;
    background-color:{BG2}; border-radius:4px;
    border-left:3px solid {ACCENT};
}}
.btn {{
    display:inline; padding:3px 10px; margin:2px 3px;
    border-radius:3px; font-size:11px; font-weight:bold;
}}
.btn-a {{ background-color:{ACCENT}; color:{BG}; }}
.btn-b {{ background-color:{SURFACE}; color:{TEXT}; }}
.btn-r {{ background-color:{RED};     color:{BG}; }}
.divider {{ height:1px; background-color:{SURFACE}; margin:6px 0; }}
.count {{ color:{OVERLAY}; font-size:10px; font-weight:normal; }}
.more {{ color:{OVERLAY}; font-size:11px; }}
"""

SECTION_META = {
    "Do now":                   ("b-bug",   "&#9888;",  "DO NOW"),
    "Bugs / Broken Things":     ("b-bug",   "&#128027;","Bugs"),
    "UI / Interaction Design":  ("b-ui",    "&#9998;",  "UI"),
    "Setup / Unknown Status":   ("b-setup", "&#9881;",  "Setup"),
    "MCP Discovery / Installation": ("b-mcp","&#9889;","MCP"),
    "Notes / Journal":          ("b-note",  "&#128214;","Notes"),
    "Done":                     ("b-note",  "&#10003;", "Done"),
}

SECTION_ORDER = [
    "Do now",
    "Bugs / Broken Things",
    "UI / Interaction Design",
    "Setup / Unknown Status",
    "MCP Discovery / Installation",
    "Notes / Journal",
    "Done",
]

MAX_PER_SECTION = 6


def build_html():
    sections = _parse_inbox()

    total_open = sum(
        1 for items in sections.values()
        for it in items if not it["done"]
    )

    parts = [f"<html><style>{CSS}</style><body>"]
    inbox_url = INBOX_PATH.as_uri()
    parts.append(f'<h1>&#9670; Claude AI Hub</h1>')
    parts.append(f'<div class="sub">Inbox &mdash; {total_open} open &nbsp;|&nbsp; '
                 f'<a href="action://refresh">&#8635; refresh</a> &nbsp;|&nbsp; '
                 f'<a href="action://open-inbox">&#128195; inbox</a></div>')

    for sec in SECTION_ORDER:
        items = sections.get(sec, [])
        if not items:
            continue

        badge_cls, icon, label = SECTION_META.get(sec, ("b-note", "", sec))
        undone = [it for it in items if not it["done"]]
        done_c = len(items) - len(undone)

        if sec == "Done" and not done_c:
            continue

        count_str = (
            f' <span class="count">({len(undone)} open'
            + (f', {done_c} done' if done_c else '')
            + ')</span>'
        )

        parts.append(
            f'<h2>'
            f'<span class="badge {badge_cls}">{icon}</span>'
            f'{_e(sec)}{count_str}'
            f'</h2>'
        )

        shown = 0
        for it in items:
            if it["done"]:
                cls = "item done"
                txt = _e(it["text"][:70])
                parts.append(f'<div class="{cls}">{txt}</div>')
                continue

            if shown >= MAX_PER_SECTION:
                rem = len(undone) - shown
                parts.append(
                    f'<div class="item more">'
                    f'<a href="action://open-inbox">&#8230; {rem} more</a>'
                    f'</div>'
                )
                break

            cls = "item urgent" if sec == "Do now" else "item"
            date = f'<span class="date">{it["date"]} </span>' if it["date"] else ""
            txt  = _e(it["text"][:90]) + ("&#8230;" if len(it["text"]) > 90 else "")

            # If it looks like a URL, make it a link
            if it["text"].startswith("http"):
                url = it["text"].split()[0]
                txt = f'<a href="{_e(url)}">{_e(url[:70])}</a>'

            parts.append(f'<div class="{cls}">{date}{txt}</div>')
            shown += 1

    # Action bar
    parts.append(f"""
<div class="actions">
  <a class="btn btn-a" href="action://refresh">&#8635; Refresh</a>
  <a class="btn btn-b" href="action://open-inbox">&#128195; Inbox</a>
  <a class="btn btn-b" href="action://config">&#9881; Config</a>
  <a class="btn btn-r" href="action://open-hub-py">&#9998; Edit</a>
</div>
""")
    parts.append("</body></html>")
    return "".join(parts)


# ── state ────────────────────────────────────────────────────────────────────

class _HubState:
    view = None          # the scratch view hosting the phantom
    phantom_set = None   # PhantomSet on that view


# ── commands ─────────────────────────────────────────────────────────────────

def _navigate(href):
    """Handle link clicks from the hub phantom."""
    w = sublime.active_window()
    if href == "action://refresh":
        w.run_command("ai_hub_refresh")
    elif href == "action://open-inbox":
        w.open_file(str(INBOX_PATH))
    elif href == "action://config":
        w.run_command("st_config_open")
    elif href == "action://open-hub-py":
        import User.ai.ai_hub as _self
        w.open_file(_self.__file__)
    elif href.startswith("http"):
        import subprocess
        subprocess.Popen(["cmd", "/c", "start", "", href])


def _render(view):
    """Push fresh HTML into the phantom set on *view*."""
    if _HubState.phantom_set is None or _HubState.view != view:
        _HubState.phantom_set = sublime.PhantomSet(view, "ai_hub")
    html = build_html()
    phantom = sublime.Phantom(
        sublime.Region(0),
        html,
        sublime.LAYOUT_BLOCK,
        on_navigate=_navigate,
    )
    _HubState.phantom_set.update([phantom])


class AiHubOpenCommand(sublime_plugin.WindowCommand):
    """Open the AI Hub dashboard panel in a right-column scratch view, or refresh it if already open.

    Key binding: ctrl+alt+h
    Command palette: "AI Hub: Open"
    """

    def run(self):
        # Find existing hub view by name (survives module reloads)
        existing = None
        for v in self.window.views():
            if v.name() == "◆ AI Hub" and v.is_valid():
                existing = v
                break

        # Close any extras beyond the first
        found = [v for v in self.window.views() if v.name() == "◆ AI Hub"]
        for dup in found[1:]:
            dup.close()

        if existing is not None:
            _HubState.view = existing
            _render(existing)
            self.window.focus_view(existing)
            return

        # Ensure 2-column layout; hub goes in right column (group 1)
        layout = self.window.get_layout()
        if len(layout.get("cols", [])) < 3:
            self.window.set_layout({
                "cols": [0.0, 0.65, 1.0],
                "rows": [0.0, 1.0],
                "cells": [[0, 0, 1, 1], [1, 0, 2, 1]],
            })

        v = self.window.new_file()
        self.window.set_view_index(v, 1, 0)
        v.set_name("◆ AI Hub")
        v.set_scratch(True)
        v.set_read_only(True)
        v.settings().set("gutter", False)
        v.settings().set("line_numbers", False)
        v.settings().set("scroll_past_end", False)
        v.settings().set("word_wrap", False)
        v.settings().set("show_minimap", False)
        v.settings().set("scroll_bar_enabled", False)

        _HubState.view = v
        _render(v)


class AiHubRefreshCommand(sublime_plugin.WindowCommand):
    """Re-render the AI Hub phantom in the existing hub view.

    Command palette: "AI Hub: Refresh"
    """

    def run(self):
        v = _HubState.view
        if v and v.is_valid():
            _render(v)


# ── status bar ───────────────────────────────────────────────────────────────

class AiHubStatusListener(sublime_plugin.EventListener):
    """Keep a live count of open inbox items in the status bar."""

    _last_count = -1

    def on_activated(self, view):
        self._update(view)

    def on_post_save(self, view):
        fname = view.file_name() or ""
        if fname.endswith("ideas_inbox.md"):
            self._update(view)
            # Auto-refresh hub sheet if open
            view.window() and view.window().run_command("ai_hub_refresh")

    def _update(self, view):
        w = view.window()
        if w is None:
            return
        try:
            sections = _parse_inbox()
            count = sum(1 for items in sections.values() for it in items if not it["done"])
            if count != self._last_count:
                self.__class__._last_count = count
                do_now = len([it for it in sections.get("Do now", []) if not it["done"]])
                label = f"Inbox: {count}"
                if do_now:
                    label += f"  ⚑{do_now} DO NOW"
                view.set_status("ai_hub_inbox", label)
        except Exception:
            pass
