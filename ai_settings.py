"""ai_settings.py — Modern ST settings browser.

Opens with Ctrl+Alt+, (comma). Shows all Preferences with
pill-button booleans, quick-panel dropdowns, and input-panel
text/number editing. Asterisk marks user-overridden values.
"""

import re
import textwrap
import sublime
import sublime_plugin

# ── known enum options for string/mixed settings ──────────────────────────────

ENUMS = {
    "caret_style":               ["smooth", "phase", "blink", "wide", "solid"],
    "auto_complete_preserve_order": ["none", "some", "strict"],
    "default_line_ending":       ["system", "windows", "unix"],
    "control_character_style":   ["hex", "abbreviation", "replacement"],
    "mini_diff":                 ["true", "false", "auto"],
    "word_wrap":                 ["true", "false", "auto"],
    "show_git_status":           ["true", "false", "auto"],
    "highlight_modified_tabs":   ["true", "false", "auto"],
    "default_dir_opener":        [],
    "draw_white_space":          ["none", "selection", "leading", "enclosed", "trailing", "isolated", "all"],
    "indent_guide_options":      [],
    "rulers":                    [],
}

# ── categories ────────────────────────────────────────────────────────────────

CATEGORIES = {
    "Font & Display": [
        "font_face", "font_size", "font_options", "line_numbers", "gutter",
        "margin", "fold_buttons", "fade_fold_buttons", "rulers",
        "draw_minimap_border", "always_show_minimap_viewport",
        "draw_white_space", "draw_unicode_white_space", "draw_indent_guides",
        "indent_guide_options", "highlight_line", "caret_style",
        "caret_extra_top", "caret_extra_bottom", "caret_extra_width",
        "block_caret", "animation_enabled",
    ],
    "Editor Behavior": [
        "tab_size", "translate_tabs_to_spaces", "use_tab_stops",
        "auto_indent", "smart_indent", "indent_to_bracket",
        "trim_trailing_white_space_on_save", "ensure_newline_at_eof_on_save",
        "default_line_ending", "word_wrap", "wrap_width",
        "word_separators", "detect_indentation",
    ],
    "Autocomplete": [
        "auto_complete", "auto_complete_delay", "auto_complete_commit_on_tab",
        "auto_complete_cycle", "auto_complete_use_history",
        "auto_complete_use_index", "auto_complete_preserve_order",
        "auto_complete_with_fields", "tab_completion",
        "auto_match_enabled", "auto_close_tags",
    ],
    "Files & Save": [
        "hot_exit", "remember_open_files", "always_prompt_for_file_reload",
        "atomic_save", "backup_on_save", "create_window_at_startup",
        "save_on_focus_lost", "close_windows_when_empty",
        "default_encoding", "fallback_encoding",
        "binary_file_patterns", "file_exclude_patterns",
        "folder_exclude_patterns",
    ],
    "UI": [
        "theme", "color_scheme", "dark_color_scheme", "light_color_scheme",
        "show_tabs", "enable_tab_scrolling", "show_encoding",
        "show_indentation", "show_line_endings", "show_sidebar",
        "sidebar_no_dir_prefix", "bold_folder_labels",
        "mouse_wheel_switches_tabs", "auto_hide_tabs", "auto_hide_menu",
        "auto_hide_status_bar", "adaptive_dividers",
        "mini_diff", "show_definitions",
    ],
    "Spell Check": [
        "spell_check", "dictionary", "added_words", "ignored_words",
    ],
    "Performance": [
        "index_files", "index_workers", "index_exclude_patterns",
        "scroll_speed", "tree_animation_enabled",
    ],
}

# ── comment parser ────────────────────────────────────────────────────────────

def _parse_descriptions():
    """Extract key→description from the default Preferences comments."""
    raw = sublime.load_resource("Packages/Default/Preferences.sublime-settings")
    descs = {}
    pending_comments = []
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("//"):
            pending_comments.append(s[2:].strip())
        elif s.startswith('"'):
            m = re.match(r'"(\w+)"\s*:', s)
            if m:
                key = m.group(1)
                desc = " ".join(pending_comments).strip()
                if len(desc) > 300:
                    desc = desc[:297] + "…"
                descs[key] = desc
                pending_comments = []
        else:
            if s not in ("{", "}"):
                pending_comments = []
    return descs


_DESCRIPTIONS = {}


def _get_desc(key):
    global _DESCRIPTIONS
    if not _DESCRIPTIONS:
        _DESCRIPTIONS = _parse_descriptions()
    return _DESCRIPTIONS.get(key, "")


# ── HTML palette ──────────────────────────────────────────────────────────────

BG      = "#1e1e2e"
BG2     = "#181825"
SURFACE = "#313244"
OVERLAY = "#6c7086"
TEXT    = "#cdd6f4"
SUBTEXT = "#a6adc8"
ACCENT  = "#cba6f7"
BLUE    = "#89b4fa"
GREEN   = "#a6e3a1"
RED     = "#f38ba8"
YELLOW  = "#f9e2af"

CSS = f"""
body {{
    background-color:{BG}; color:{TEXT};
    font-family:"Cascadia Code","Fira Code",Consolas,monospace;
    font-size:12px; margin:0; padding:12px 16px 30px;
}}
h1 {{ color:{ACCENT}; font-size:16px; margin:0 0 2px; }}
.sub {{ color:{SUBTEXT}; font-size:10px; margin:0 0 10px; }}
h2 {{
    color:{BLUE}; font-size:10px; font-weight:bold;
    margin:14px 0 4px; padding:2px 8px;
    background-color:{SURFACE}; border-radius:3px;
    text-transform:uppercase; letter-spacing:0.5px;
}}
.row {{
    padding:3px 0 3px 4px; margin:1px 0;
    border-left:2px solid {BG2};
}}
.row:hover {{ border-left:2px solid {OVERLAY}; }}
.modified {{ border-left:2px solid {ACCENT}; }}
.key {{ color:{TEXT}; font-weight:bold; font-size:11px; }}
.key-mod {{ color:{ACCENT}; font-weight:bold; font-size:11px; }}
.desc {{ color:{OVERLAY}; font-size:10px; display:block; margin:1px 0 4px 0; padding-right:8px; }}
.pill {{
    display:inline; padding:2px 8px; border-radius:3px;
    font-size:10px; font-weight:bold; margin-right:3px;
    color:{BG};
}}
.pill-on  {{ background-color:{GREEN}; }}
.pill-off {{ background-color:{SURFACE}; color:{SUBTEXT}; }}
.pill-active {{ background-color:{ACCENT}; }}
.pill-inactive {{ background-color:{SURFACE}; color:{SUBTEXT}; }}
.val {{
    display:inline; padding:2px 8px; border-radius:3px;
    font-size:10px; background-color:{SURFACE}; color:{TEXT};
    margin-right:4px;
}}
.star {{ color:{ACCENT}; font-size:10px; }}
a {{ color:{BLUE}; text-decoration:none; }}
.actions {{
    margin-top:16px; padding:6px 8px;
    background-color:{BG2}; border-radius:4px;
    border-left:3px solid {ACCENT};
}}
.btn {{
    display:inline; padding:2px 8px; border-radius:3px;
    font-size:11px; font-weight:bold; margin-right:6px;
}}
.btn-a {{ background-color:{ACCENT}; color:{BG}; }}
.btn-b {{ background-color:{SURFACE}; color:{TEXT}; }}
.filter-active {{ color:{YELLOW}; font-size:10px; }}
"""


def _e(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── state ─────────────────────────────────────────────────────────────────────

class _State:
    view = None
    phantom_set = None
    filter_text = ""
    active_category = None   # None = all


# ── HTML builder ──────────────────────────────────────────────────────────────

def _render_row(key, default_val, user_prefs, parts, wrap_cols=50):
    current = user_prefs.get(key)
    is_modified = current is not None and current != default_val
    effective = current if current is not None else default_val

    star = '<span class="star"> &#9733;</span>' if is_modified else ""
    row_cls = "row modified" if is_modified else "row"
    key_cls = "key-mod" if is_modified else "key"

    desc = _get_desc(key)
    if desc:
        wrapped = "<br>".join(_e(line) for line in textwrap.wrap(desc, wrap_cols))
        desc_html = f'<div class="desc">{wrapped}</div>'
    else:
        desc_html = ""

    # ── boolean ──────────────────────────────────────────────────────────────
    if isinstance(default_val, bool):
        on_cls  = "pill pill-on"  if effective is True  else "pill pill-off"
        off_cls = "pill pill-off" if effective is True  else "pill pill-on"
        # clicking the inactive pill sets it
        if effective is True:
            ctrl = (
                f'<a href="action://set/{key}/true">'
                f'<span class="{on_cls}">&#10003; True</span></a> '
                f'<a href="action://set/{key}/false">'
                f'<span class="{off_cls}">False</span></a>'
            )
        else:
            ctrl = (
                f'<a href="action://set/{key}/true">'
                f'<span class="{off_cls}">True</span></a> '
                f'<a href="action://set/{key}/false">'
                f'<span class="{on_cls}">&#10007; False</span></a>'
            )

    # ── known enum ────────────────────────────────────────────────────────────
    elif key in ENUMS and ENUMS[key]:
        options = ENUMS[key]
        pills = []
        for opt in options:
            active = str(effective).lower() == str(opt).lower()
            cls = "pill pill-active" if active else "pill pill-inactive"
            pills.append(
                f'<a href="action://set/{key}/{opt}">'
                f'<span class="{cls}">{_e(opt)}</span></a>'
            )
        ctrl = " ".join(pills)

    # ── integer ───────────────────────────────────────────────────────────────
    elif isinstance(default_val, int):
        ctrl = (
            f'<a href="action://input/{key}">'
            f'<span class="val">{_e(effective)}</span></a>'
        )

    # ── string ────────────────────────────────────────────────────────────────
    elif isinstance(default_val, str):
        disp = str(effective)[:30]
        ctrl = (
            f'<a href="action://input/{key}">'
            f'<span class="val">{_e(disp)}</span></a>'
        )

    # ── list / other ──────────────────────────────────────────────────────────
    else:
        disp = str(effective)[:40]
        ctrl = (
            f'<a href="action://input/{key}">'
            f'<span class="val">{_e(disp)}</span></a>'
        )

    parts.append(
        f'<div class="{row_cls}">'
        f'<div><span class="{key_cls}">{_e(key)}{star}</span> {ctrl}</div>'
        f'{desc_html}'
        f'</div>'
    )


def build_settings_html(width_px=460, em_width=9.0):
    wrap_cols = max(30, int((width_px - 16) / em_width))
    prefs_raw = sublime.load_resource("Packages/Default/Preferences.sublime-settings")
    defaults  = sublime.decode_value(prefs_raw)
    user_prefs = sublime.load_settings("Preferences.sublime-settings")

    flt = _State.filter_text.lower()
    cat = _State.active_category

    modified_count = sum(
        1 for k in defaults
        if user_prefs.get(k) is not None and user_prefs.get(k) != defaults[k]
    )

    parts = [f'<html><style>{CSS}</style><body style="max-width:{width_px}px">']
    parts.append('<h1>&#9881; ST Settings</h1>')

    filter_display = (
        f' &nbsp;<span class="filter-active">filter: {_e(flt)}</span>'
        if flt else ""
    )
    parts.append(
        f'<div class="sub">'
        f'{modified_count} modified &nbsp;|&nbsp; '
        f'<a href="action://search">&#128269; search</a> &nbsp;|&nbsp; '
        f'<a href="action://clear-filter">clear</a> &nbsp;|&nbsp; '
        f'<a href="action://hub">&#9670; Hub</a>'
        f'{filter_display}'
        f'</div>'
    )

    # category filter pills
    parts.append('<div style="margin-bottom:8px;">')
    all_cls = "pill pill-active" if cat is None else "pill pill-inactive"
    parts.append(f'<a href="action://cat/"><span class="{all_cls}">All</span></a> ')
    for cname in CATEGORIES:
        c_cls = "pill pill-active" if cat == cname else "pill pill-inactive"
        safe = cname.replace(" ", "_").replace("&", "and")
        parts.append(f'<a href="action://cat/{safe}"><span class="{c_cls}">{_e(cname)}</span></a> ')
    parts.append('</div>')

    # Determine which keys to show and in what order
    if cat:
        cat_keys = CATEGORIES.get(cat, [])
        show_order = [(cat, cat_keys)]
    else:
        show_order = list(CATEGORIES.items())
        # Add uncategorised keys
        categorised = {k for keys in CATEGORIES.values() for k in keys}
        other = [k for k in defaults if k not in categorised]
        if other:
            show_order.append(("Other", other))

    for section, keys in show_order:
        section_rows = []
        for key in keys:
            if key not in defaults:
                continue
            if flt and flt not in key.lower() and flt not in _get_desc(key).lower():
                continue
            section_rows.append(key)

        if not section_rows:
            continue

        parts.append(f'<h2>{_e(section)}</h2>')
        for key in section_rows:
            _render_row(key, defaults[key], user_prefs, parts, wrap_cols)

    parts.append("""
<div class="actions">
  <a class="btn btn-b" href="action://open-raw">&#128196; Open Raw JSON</a>
  <a class="btn btn-b" href="action://hub">&#9670; Hub</a>
</div>
</body></html>""")
    return "".join(parts)


# ── navigation handler ────────────────────────────────────────────────────────

def _navigate(href):
    w = sublime.active_window()
    defaults_raw = sublime.load_resource("Packages/Default/Preferences.sublime-settings")
    defaults = sublime.decode_value(defaults_raw)
    user_prefs = sublime.load_settings("Preferences.sublime-settings")

    if href == "action://search":
        w.show_input_panel(
            "Filter settings:",
            _State.filter_text,
            lambda s: _apply_filter(s),
            None, None
        )
        return

    if href == "action://clear-filter":
        _State.filter_text = ""
        _State.active_category = None
        _refresh()
        return

    if href == "action://hub":
        w.run_command("ai_hub_open")
        return

    if href == "action://open-raw":
        w.run_command("open_file", {"file": "${packages}/User/Preferences.sublime-settings"})
        return

    if href.startswith("action://cat/"):
        raw_cat = href[len("action://cat/"):]
        if not raw_cat:
            _State.active_category = None
        else:
            # Reverse map safe name → real category name
            for cname in CATEGORIES:
                safe = cname.replace(" ", "_").replace("&", "and")
                if safe == raw_cat:
                    _State.active_category = cname
                    break
        _refresh()
        return

    if href.startswith("action://set/"):
        rest = href[len("action://set/"):]
        slash = rest.index("/")
        key   = rest[:slash]
        value = rest[slash+1:]

        if key not in defaults:
            return

        default_val = defaults[key]
        if isinstance(default_val, bool):
            typed_val = value.lower() == "true"
        elif isinstance(default_val, int):
            try:
                typed_val = int(value)
            except ValueError:
                return
        else:
            # mixed bool/string (mini_diff, word_wrap etc.)
            if value.lower() == "true":
                typed_val = True
            elif value.lower() == "false":
                typed_val = False
            else:
                typed_val = value

        user_prefs.set(key, typed_val)
        sublime.save_settings("Preferences.sublime-settings")
        _refresh()
        return

    if href.startswith("action://input/"):
        key = href[len("action://input/"):]
        if key not in defaults:
            return
        current = user_prefs.get(key)
        effective = current if current is not None else defaults[key]

        w.show_input_panel(
            f"{key}:",
            str(effective),
            lambda s: _apply_input(key, s, defaults[key]),
            None, None
        )
        return


def _apply_filter(text):
    _State.filter_text = text.strip()
    _refresh()


def _apply_input(key, raw, default_val):
    user_prefs = sublime.load_settings("Preferences.sublime-settings")
    raw = raw.strip()
    if isinstance(default_val, bool):
        typed = raw.lower() in ("true", "1", "yes")
    elif isinstance(default_val, int):
        try:
            typed = int(raw)
        except ValueError:
            sublime.status_message(f"Invalid integer: {raw}")
            return
    elif isinstance(default_val, list):
        try:
            typed = sublime.decode_value(raw)
        except Exception:
            sublime.status_message(f"Invalid JSON: {raw}")
            return
    else:
        typed = raw
    user_prefs.set(key, typed)
    sublime.save_settings("Preferences.sublime-settings")
    _refresh()


def _refresh():
    v = _State.view
    if v and v.is_valid():
        if _State.phantom_set is None:
            _State.phantom_set = sublime.PhantomSet(v, "ai_settings")
        width_px = max(200, int(v.viewport_extent()[0]) - 52)
        html = build_settings_html(width_px, v.em_width())
        phantom = sublime.Phantom(
            sublime.Region(0),
            html,
            sublime.LAYOUT_BLOCK,
            on_navigate=_navigate,
        )
        _State.phantom_set.update([phantom])


# ── commands ──────────────────────────────────────────────────────────────────

class AiSettingsOpenCommand(sublime_plugin.WindowCommand):
    """Open the ST Settings browser. Ctrl+Alt+, """

    def run(self):
        # Find existing view
        existing = None
        dupes = []
        for v in self.window.views():
            if v.name() == "⚙ ST Settings":
                if existing is None:
                    existing = v
                else:
                    dupes.append(v)
        for d in dupes:
            d.close()

        if existing and existing.is_valid():
            _State.view = existing
            _refresh()
            self.window.focus_view(existing)
            return

        # 2-column layout
        layout = self.window.get_layout()
        if len(layout.get("cols", [])) < 3:
            self.window.set_layout({
                "cols": [0.0, 0.65, 1.0],
                "rows": [0.0, 1.0],
                "cells": [[0, 0, 1, 1], [1, 0, 2, 1]],
            })

        v = self.window.new_file()
        self.window.set_view_index(v, 1, 0)
        v.set_name("⚙ ST Settings")
        v.set_scratch(True)
        v.set_read_only(True)
        v.settings().set("gutter", False)
        v.settings().set("line_numbers", False)
        v.settings().set("scroll_past_end", False)
        v.settings().set("show_minimap", False)
        v.settings().set("scroll_bar_enabled", False)

        _State.view = v
        _State.phantom_set = sublime.PhantomSet(v, "ai_settings")
        _refresh()


class AiSettingsListener(sublime_plugin.EventListener):
    """Re-render on focus so column resizes are picked up."""

    def on_activated(self, view):
        if view.name() == "⚙ ST Settings" and view.is_valid():
            _State.view = view
            _refresh()
