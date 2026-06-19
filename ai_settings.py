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


# ── HTML palette (Windows light theme) ───────────────────────────────────────

CSS = """
body {
    background-color:#f0f0f0;
    font-family:"Segoe UI",Arial,sans-serif;
    font-size:9pt; color:#000000;
    margin:0; padding:8px 12px 20px;
}
h1 { font-size:11pt; color:#003366; margin:0 0 2px; font-weight:bold; }
.sub { color:#555555; font-size:8pt; margin:0 0 6px; }
.toolbar {
    margin-bottom:8px; padding:4px 8px;
    background:#e1e1e1; border:1px solid #adadad;
}
.section-hdr {
    font-size:9pt; font-weight:bold; color:#003366;
    background:#dce6f0; padding:3px 8px;
    margin:10px 0 0;
    border-top:1px solid #adadad; border-bottom:1px solid #adadad;
}
table { width:100%; border-collapse:collapse; background:#ffffff; border:1px solid #adadad; margin-bottom:2px; }
tr { border-bottom:1px solid #ebebeb; }
td.lbl  { width:42%; padding:4px 6px 2px 6px; vertical-align:top; border-right:1px solid #e0e0e0; }
td.lbl2 { width:42%; padding:4px 6px 2px 6px; vertical-align:top; border-right:1px solid #e0e0e0; background:#fffde7; }
td.ctrl { width:58%; padding:3px 6px; vertical-align:middle; }
.key    { font-size:9pt; color:#000000; font-weight:bold; }
.key-mod{ font-size:9pt; color:#0000cc; font-weight:bold; }
.desc   { font-size:8pt; color:#666666; display:block; margin-top:1px; }
.star   { color:#cc6600; }
.chk    { font-size:11pt; color:#000000; }
.textbox{
    display:inline; background:#ffffff;
    border:1px solid #7a7a7a; padding:1px 6px;
    color:#000000; font-family:"Segoe UI",Arial,sans-serif; font-size:9pt;
}
.dropdown{
    display:inline; background:#ffffff;
    border:1px solid #7a7a7a; padding:1px 4px 1px 6px;
    color:#000000; font-family:"Segoe UI",Arial,sans-serif; font-size:9pt;
}
.catbar { margin-bottom:6px; }
.catbtn {
    display:inline; padding:2px 9px;
    border:1px solid #adadad; background:#e1e1e1;
    font-size:8pt; margin-right:3px; color:#000000;
}
.catbtn-on { background:#0078d4; color:#ffffff; border:1px solid #005a9e; }
.btn {
    display:inline; padding:2px 10px;
    border:1px solid #adadad; background:#e1e1e1;
    font-size:9pt; margin-right:4px; color:#000000;
}
a { color:#0078d4; text-decoration:none; }
.filter-active { color:#cc6600; font-size:8pt; }
.actions {
    margin-top:12px; padding:5px 8px;
    background:#e8e8e8; border:1px solid #adadad;
}
"""


def _e(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── state ─────────────────────────────────────────────────────────────────────

PREFS_RESOURCE = "Packages/Default/Preferences.sublime-settings"


def _get_all_package_settings():
    """Return sorted list of (label, resource_path) for all non-User settings."""
    results = []
    seen = set()
    for path in sublime.find_resources("*.sublime-settings"):
        m = re.match(r"Packages/([^/]+)/(.+\.sublime-settings)$", path)
        if not m or m.group(1) == "User":
            continue
        pkg  = m.group(1)
        fname = m.group(2)
        label = f"{pkg}  —  {fname}"
        if label not in seen:
            seen.add(label)
            results.append((label, path))
    results.sort(key=lambda x: x[0].lower())
    return results


class _State:
    view = None
    phantom_set = None
    filter_text = ""
    minimap_was_visible = False
    active_category = None        # None = all
    settings_resource = PREFS_RESOURCE   # which file we're browsing


# ── HTML builder ──────────────────────────────────────────────────────────────

def _render_row(key, default_val, user_prefs, parts, wrap_cols=50):
    current = user_prefs.get(key)
    is_modified = current is not None and current != default_val
    effective = current if current is not None else default_val

    star    = ' <span class="star">&#9733;</span>' if is_modified else ""
    lbl_cls = "lbl2" if is_modified else "lbl"
    key_cls = "key-mod" if is_modified else "key"

    desc = _get_desc(key)
    desc_html = f'<span class="desc">{_e(desc[:110])}</span>' if desc else ""

    # ── boolean: checkbox ─────────────────────────────────────────────────────
    if isinstance(default_val, bool):
        symbol  = "&#9745;" if effective else "&#9744;"   # ☑ / ☐
        new_val = "false" if effective else "true"
        ctrl = (
            f'<a href="action://set/{key}/{new_val}">'
            f'<span class="chk">{symbol}</span></a>'
        )

    # ── known enum: dropdown ──────────────────────────────────────────────────
    elif key in ENUMS and ENUMS[key]:
        ctrl = (
            f'<a href="action://enum/{key}">'
            f'<span class="dropdown">{_e(str(effective))}&nbsp;&#9660;</span></a>'
        )

    # ── integer / string / other: text box ────────────────────────────────────
    else:
        disp = str(effective)[:30]
        ctrl = (
            f'<a href="action://input/{key}">'
            f'<span class="textbox">{_e(disp)}</span></a>'
        )

    parts.append(
        f'<tr>'
        f'<td class="{lbl_cls}"><span class="{key_cls}">{_e(key)}{star}</span>{desc_html}</td>'
        f'<td class="ctrl">{ctrl}</td>'
        f'</tr>'
    )


def build_settings_html(width_px=460, em_width=9.0):
    wrap_cols = max(30, int((width_px - 16) / 6.0))
    resource  = _State.settings_resource
    is_prefs  = (resource == PREFS_RESOURCE)

    try:
        raw = sublime.load_resource(resource)
        defaults = sublime.decode_value(raw)
    except Exception:
        defaults = {}

    # User overrides live in User/<filename>
    settings_fname = resource.split("/")[-1]
    user_prefs = sublime.load_settings(settings_fname)

    flt = _State.filter_text.lower()
    cat = _State.active_category if is_prefs else None

    modified_count = sum(
        1 for k in defaults
        if user_prefs.get(k) is not None and user_prefs.get(k) != defaults[k]
    )

    # Title: package name + file
    m = re.match(r"Packages/([^/]+)/(.+)", resource)
    pkg_name  = m.group(1) if m else "Settings"
    file_name = m.group(2) if m else settings_fname
    title = f"&#9881; {_e(pkg_name)}" if is_prefs else f"&#9881; {_e(pkg_name)} &mdash; {_e(file_name)}"

    parts = [f'<html><style>{CSS}</style><body style="max-width:{width_px}px">']
    parts.append(f'<h1>&#9881; {title}</h1>')

    filter_display = f'&nbsp; <span class="filter-active">filter: {_e(flt)}</span>' if flt else ""
    back = f'<a class="btn" href="action://prefs">&#8592; ST Prefs</a> ' if not is_prefs else ''
    parts.append(
        f'<div class="toolbar">'
        f'{back}'
        f'<a class="btn" href="action://packages">Packages</a> '
        f'<a class="btn" href="action://search">Search</a> '
        f'<a class="btn" href="action://clear-filter">Clear Filter</a> '
        f'<a class="btn" href="action://open-raw">Open JSON</a> '
        f'<a class="btn" href="action://hub">Hub</a>'
        f'&nbsp;&nbsp;<span class="sub">{modified_count} modified{filter_display}</span>'
        f'</div>'
    )

    # Category filter bar — only for main Preferences
    if is_prefs:
        cat_names = list(CATEGORIES.keys())
        all_cls = "catbtn catbtn-on" if cat is None else "catbtn"
        parts.append(f'<div class="catbar"><a class="{all_cls}" href="action://cat/">All</a> ')
        for cname in cat_names:
            c_cls = "catbtn catbtn-on" if cat == cname else "catbtn"
            safe = cname.replace(" ", "_").replace("&", "and")
            parts.append(f'<a class="{c_cls}" href="action://cat/{safe}">{_e(cname)}</a> ')
        parts.append('</div>')

    # Determine which keys to show
    if is_prefs and cat:
        show_order = [(cat, CATEGORIES[cat])]
    elif is_prefs:
        show_order = list(CATEGORIES.items())
        categorised = {k for keys in CATEGORIES.values() for k in keys}
        other = [k for k in defaults if k not in categorised]
        if other:
            show_order.append(("Other", other))
    else:
        show_order = [("Settings", list(defaults.keys()))]

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

        parts.append(f'<div class="section-hdr">{_e(section)}</div>')
        parts.append('<table>')
        for key in section_rows:
            _render_row(key, defaults[key], user_prefs, parts, wrap_cols)
        parts.append('</table>')

    parts.append('</body></html>')
    return "".join(parts)


# ── navigation handler ────────────────────────────────────────────────────────

def _navigate(href):
    w = sublime.active_window()
    try:
        defaults_raw = sublime.load_resource(_State.settings_resource)
        defaults = sublime.decode_value(defaults_raw)
    except Exception:
        defaults = {}
    settings_fname = _State.settings_resource.split("/")[-1]
    user_prefs = sublime.load_settings(settings_fname)

    if href == "action://packages":
        pkg_list = _get_all_package_settings()
        labels  = [p[0] for p in pkg_list]
        paths   = [p[1] for p in pkg_list]
        def on_select(idx):
            if idx < 0:
                return
            _State.settings_resource = paths[idx]
            _State.active_category = None
            _State.filter_text = ""
            _refresh()
        w.show_quick_panel(labels, on_select)
        return

    if href == "action://prefs":
        _State.settings_resource = PREFS_RESOURCE
        _State.active_category = None
        _State.filter_text = ""
        _refresh()
        return

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
        w.run_command("edit_settings", {"base_file": "${packages}/" + "/".join(_State.settings_resource.split("/")[1:])})
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
        sublime.save_settings(settings_fname)
        _refresh()
        return

    if href.startswith("action://enum/"):
        key = href[len("action://enum/"):]
        if key not in ENUMS or not ENUMS[key]:
            return
        options = ENUMS[key]
        current = user_prefs.get(key)
        effective = current if current is not None else defaults.get(key, options[0])
        try:
            selected = options.index(str(effective))
        except ValueError:
            selected = 0

        def on_done(idx):
            if idx < 0:
                return
            settings_fname = _State.settings_resource.split("/")[-1]
            up = sublime.load_settings(settings_fname)
            up.set(key, options[idx])
            sublime.save_settings(settings_fname)
            _refresh()

        w.show_quick_panel(options, on_done, selected_index=selected)
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
    settings_fname = _State.settings_resource.split("/")[-1]
    user_prefs = sublime.load_settings(settings_fname)
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
    sublime.save_settings(settings_fname)
    _refresh()


def _refresh():
    v = _State.view
    if v is None or not v.is_valid():
        for w in sublime.windows():
            for vv in w.views():
                if vv.name() == "⚙ ST Settings" and vv.is_valid():
                    v = vv
                    _State.view = v
                    break
            if v and v.is_valid():
                break
    if v and v.is_valid():
        if _State.phantom_set is None:
            _State.phantom_set = sublime.PhantomSet(v, "ai_settings")
        w = v.window()
        minimap_w = 66 if (w and w.is_minimap_visible()) else 0
        width_px = max(200, int(v.viewport_extent()[0]) - minimap_w - 24)
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
            _State.settings_resource = PREFS_RESOURCE
            _State.active_category = None
            _State.filter_text = ""
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
        v.settings().set("scroll_bar_enabled", False)

        _State.view = v
        _State.phantom_set = sublime.PhantomSet(v, "ai_settings")
        sublime.set_timeout(_refresh, 150)


class AiSettingsListener(sublime_plugin.EventListener):
    """Re-render on focus; manage minimap for clean display."""

    def on_activated(self, view):
        if view.name() == "⚙ ST Settings" and view.is_valid():
            _State.view = view
            _refresh()
