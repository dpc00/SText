"""st_settings.py — ST Settings browser, fully self-contained.

Runs an HTTP server in-process (ST's own Python, no subprocess).
Opens the settings UI in the default browser.
"""

import json
import os
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import sublime
import sublime_plugin

# ── known enum options ─────────────────────────────────────────────────────────

ENUMS = {
    "caret_style":                  ["smooth", "phase", "blink", "wide", "solid"],
    "auto_complete_preserve_order": ["none", "some", "strict"],
    "default_line_ending":          ["system", "windows", "unix"],
    "control_character_style":      ["hex", "abbreviation", "replacement"],
    "mini_diff":                    ["true", "false", "auto"],
    "word_wrap":                    ["true", "false", "auto"],
    "show_git_status":              ["true", "false", "auto"],
    "highlight_modified_tabs":      ["true", "false", "auto"],
    "draw_white_space":             ["none", "selection", "leading", "enclosed", "trailing", "isolated", "all"],
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

PREFS_RESOURCE = "Packages/Default/Preferences.sublime-settings"


# ── data builders ─────────────────────────────────────────────────────────────

def _get_all_package_settings():
    results = []
    seen = set()
    for path in sublime.find_resources("*.sublime-settings"):
        m = re.match(r"Packages/([^/]+)/(.+\.sublime-settings)$", path)
        if not m or m.group(1) == "User":
            continue
        pkg = m.group(1)
        fname = m.group(2)
        label = f"{pkg}  —  {fname}"
        if label not in seen:
            seen.add(label)
            results.append((label, path))
    results.sort(key=lambda x: x[0].lower())
    return results


def _build_all_keys_index():
    results = []
    seen = set()
    for path in sublime.find_resources("*.sublime-settings"):
        m = re.match(r"Packages/([^/]+)/(.+\.sublime-settings)$", path)
        if not m or m.group(1) == "User":
            continue
        pkg = m.group(1)
        fname = m.group(2)
        pkg_label = f"{pkg} — {fname}"
        try:
            raw = sublime.load_resource(path)
            keys = sublime.decode_value(raw)
            if not isinstance(keys, dict):
                continue
        except Exception:
            continue
        for key in keys:
            uid = f"{path}::{key}"
            if uid not in seen:
                seen.add(uid)
                results.append({"key": key, "resource": path, "pkg": pkg_label})
    return results


_FONT_FALLBACK = [
    "Cascadia Code", "Cascadia Mono", "Consolas", "Courier New",
    "DejaVu Sans Mono", "Fira Code", "Inconsolata", "JetBrains Mono",
    "Lucida Console", "Menlo", "Monaco", "Roboto Mono", "Source Code Pro",
]


def _family_from_filename(fname):
    name = os.path.splitext(fname)[0]
    name = re.sub(
        r'[-_ ](Regular|Bold|Italic|Light|Medium|SemiBold|ExtraLight|'
        r'Thin|Black|Heavy|Condensed|Oblique).*$',
        '', name, flags=re.IGNORECASE)
    return name.strip()


def _get_installed_fonts(current_font=""):
    fonts = set()
    try:
        if sys.platform == "win32":
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    key = winreg.OpenKey(hive,
                        r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
                    i = 0
                    while True:
                        try:
                            name, _, _ = winreg.EnumValue(key, i)
                            name = re.sub(r'\s*\(.*?\)\s*$', '', name).strip()
                            if name:
                                fonts.add(name)
                            i += 1
                        except OSError:
                            break
                    winreg.CloseKey(key)
                except OSError:
                    pass

        elif sys.platform == "darwin":
            dirs = ["/System/Library/Fonts", "/Library/Fonts",
                    os.path.expanduser("~/Library/Fonts")]
            for d in dirs:
                try:
                    for f in os.listdir(d):
                        if f.lower().endswith((".ttf", ".otf", ".ttc")):
                            fonts.add(_family_from_filename(f))
                except OSError:
                    pass

        else:
            try:
                import subprocess
                out = subprocess.check_output(
                    ["fc-list", "--format=%{family}\n"],
                    timeout=3, text=True, stderr=subprocess.DEVNULL)
                for line in out.splitlines():
                    name = line.strip().split(",")[0].strip()
                    if name:
                        fonts.add(name)
            except Exception:
                for d in ["/usr/share/fonts", "/usr/local/share/fonts",
                          os.path.expanduser("~/.fonts"),
                          os.path.expanduser("~/.local/share/fonts")]:
                    try:
                        for root, _, files in os.walk(d):
                            for f in files:
                                if f.lower().endswith((".ttf", ".otf")):
                                    fonts.add(_family_from_filename(f))
                    except OSError:
                        pass

    except Exception:
        pass

    if not fonts:
        fonts = set(_FONT_FALLBACK)

    result = sorted(fonts, key=str.lower)
    if current_font and current_font not in fonts:
        result.insert(0, current_font)
    return result


_DESCRIPTIONS = {}


def _get_descriptions():
    global _DESCRIPTIONS
    if _DESCRIPTIONS:
        return _DESCRIPTIONS
    try:
        raw = sublime.load_resource(PREFS_RESOURCE)
        pending = []
        for line in raw.splitlines():
            s = line.strip()
            if s.startswith("//"):
                pending.append(s[2:].strip())
            elif s.startswith('"'):
                m = re.match(r'"(\w+)"\s*:', s)
                if m:
                    _DESCRIPTIONS[m.group(1)] = " ".join(pending).strip()
                    pending = []
            else:
                if s not in ("{", "}"):
                    pending = []
    except Exception:
        pass
    return _DESCRIPTIONS


def _build_data(settings_resource):
    try:
        raw = sublime.load_resource(settings_resource)
        defaults = sublime.decode_value(raw)
    except Exception:
        defaults = {}

    settings_fname = settings_resource.split("/")[-1]
    try:
        user_raw = sublime.load_resource(f"Packages/User/{settings_fname}")
        user_prefs = sublime.decode_value(user_raw)
    except Exception:
        user_prefs = {}

    descs = _get_descriptions()
    is_prefs = (settings_resource == PREFS_RESOURCE)

    if is_prefs:
        categorised = {k for ks in CATEGORIES.values() for k in ks}
        other = [k for k in defaults if k not in categorised]
        show_order = list(CATEGORIES.items())
        if other:
            show_order.append(("Other", other))
    else:
        show_order = [("Settings", list(defaults.keys()))]

    live = sublime.load_settings(settings_fname)
    m = re.match(r"Packages/([^/]+)/(.+)", settings_resource)
    pkg_name  = m.group(1) if m else "Settings"
    file_name = m.group(2) if m else settings_fname
    package_label = "Sublime Text Preferences" if is_prefs else f"{pkg_name} — {file_name}"

    current_font = live.get("font_face", "")
    mono_fonts = _get_installed_fonts(current_font)

    return {
        "defaults": defaults,
        "user_prefs": user_prefs,
        "effective": {"font_face": current_font},
        "mono_fonts": mono_fonts,
        "descriptions": {k: descs.get(k, "") for k in defaults},
        "enums": ENUMS,
        "categories": {k: list(v) for k, v in CATEGORIES.items()} if is_prefs else {},
        "show_order": [[s, ks] for s, ks in show_order],
        "settings_fname": settings_fname,
        "settings_resource": settings_resource,
        "package_label": package_label,
        "package_list": [{"label": lbl, "resource": res}
                         for lbl, res in _get_all_package_settings()],
        "is_prefs": is_prefs,
        "prefs_resource": PREFS_RESOURCE,
        "all_keys": _build_all_keys_index(),
    }


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sublime Text Settings</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --accent:#0078d4;--accent-dk:#106ebe;--accent-lt:#e5f3ff;
  --bg:#f3f3f3;--panel:#fff;--border:#d1d1d1;--border-lt:#ebebeb;
  --text:#1b1b1b;--muted:#6e6e6e;
  --mod-bg:#fffbe6;--mod-bdr:#f0a500;
  --hdr:52px;--foot:44px;--side:190px;
}
html,body{height:100%;overflow:hidden}
body{font-family:"Segoe UI",system-ui,sans-serif;font-size:13px;
     background:var(--bg);color:var(--text);display:flex;flex-direction:column}

/* ── Header ── */
header{height:var(--hdr);background:var(--accent);color:#fff;display:flex;
       align-items:center;padding:0 16px;gap:10px;flex-shrink:0;
       box-shadow:0 2px 6px rgba(0,0,0,.25)}
header h1{font-size:15px;font-weight:600;white-space:nowrap;letter-spacing:.2px}
.search-wrap{position:relative;flex:1;max-width:340px}
.search-wrap svg{position:absolute;left:9px;top:50%;transform:translateY(-50%);
                 opacity:.75;pointer-events:none}
#search{width:100%;padding:6px 10px 6px 32px;border:none;border-radius:4px;
        background:rgba(255,255,255,.18);color:#fff;font-size:13px;
        font-family:inherit;outline:none}
#search::placeholder{color:rgba(255,255,255,.6)}
#search:focus{background:rgba(255,255,255,.28);box-shadow:0 0 0 2px rgba(255,255,255,.4)}
.spacer{flex:1}
.hdr-info{font-size:12px;opacity:.8;white-space:nowrap}
.close-btn{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.35);
           padding:6px 18px;border-radius:4px;cursor:pointer;font-size:13px;
           font-family:inherit;transition:background .15s}
.close-btn:hover{background:rgba(255,255,255,.28)}

/* ── Body ── */
.body{display:flex;flex:1;overflow:hidden}

/* ── Sidebar ── */
nav{width:var(--side);background:#fafafa;border-right:1px solid var(--border);
    overflow-y:auto;flex-shrink:0;padding:6px 0}
.nav-item{display:flex;align-items:center;padding:7px 14px;cursor:pointer;
          border-left:3px solid transparent;font-size:12.5px;color:var(--text);
          user-select:none;transition:background .1s}
.nav-item:hover{background:#f0f0f0}
.nav-item.active{background:var(--accent-lt);border-left-color:var(--accent);
                 color:var(--accent);font-weight:600}
.nav-label{flex:1}
.nav-badge{background:var(--accent);color:#fff;font-size:10px;border-radius:10px;
           padding:1px 6px;min-width:18px;text-align:center;display:none}
.nav-badge.visible{display:inline-block}

/* ── Main ── */
main{flex:1;overflow-y:auto;background:var(--bg)}
.section{margin-bottom:1px}
.sec-hdr{position:sticky;top:0;z-index:5;background:#e8e8e8;
         border-bottom:1px solid var(--border);border-top:1px solid var(--border);
         padding:5px 16px;font-size:10.5px;font-weight:700;
         text-transform:uppercase;letter-spacing:.6px;color:var(--muted)}
table{width:100%;border-collapse:collapse;background:var(--panel)}
tr{border-bottom:1px solid var(--border-lt);transition:background .08s}
tr:hover{background:#f8f8f8}
tr.modified{background:var(--mod-bg)}
tr.modified:hover{background:#fff5cc}
tr.modified td.lbl{border-left:3px solid var(--mod-bdr)}
tr.hidden{display:none}
td.lbl{width:42%;padding:7px 12px 5px 14px;vertical-align:top}
td.ctrl{width:58%;padding:5px 12px;vertical-align:middle}
.key{font-family:"Cascadia Code","Consolas","Courier New",monospace;
     font-size:12px;font-weight:600;color:var(--text)}
.desc{font-size:11px;color:var(--muted);margin-top:2px;line-height:1.45;
      max-width:340px;display:-webkit-box;-webkit-line-clamp:4;
      -webkit-box-orient:vertical;overflow:hidden}
.reset-lnk{font-size:11px;color:var(--accent);cursor:pointer;margin-left:8px;
            display:none;background:none;border:none;font-family:inherit;
            text-decoration:underline}
tr.modified .reset-lnk{display:inline}

/* ── Controls ── */
input[type=text],input[type=number],select{
  border:1px solid #b3b3b3;border-radius:3px;padding:4px 8px;
  font-size:13px;font-family:inherit;background:#fff;color:var(--text);
  min-width:180px;max-width:320px}
input[type=text]:focus,input[type=number]:focus,select:focus{
  outline:none;border-color:var(--accent);
  box-shadow:0 0 0 2px rgba(0,120,212,.2)}
input[type=checkbox]{width:16px;height:16px;cursor:pointer;
                     accent-color:var(--accent);vertical-align:middle}
.ctrl-row{display:flex;align-items:center;gap:4px}

/* ── Footer ── */
footer{height:var(--foot);background:var(--panel);
       border-top:1px solid var(--border);display:flex;
       align-items:center;padding:0 16px;flex-shrink:0;gap:12px}
#status-msg{font-size:12px;color:var(--muted)}
#status-msg.ok{color:#107c10}
#status-msg.err{color:#c42b1c}
.mod-count{font-size:12px;color:var(--muted)}

/* ── No results ── */
.no-results{padding:40px 16px;text-align:center;color:var(--muted)}

/* ── Cross-package search results ── */
#xpkg-panel{display:none;position:absolute;top:var(--hdr);left:var(--side);right:0;
            background:#fff;border-bottom:1px solid var(--border);
            box-shadow:0 4px 12px rgba(0,0,0,.15);z-index:50;max-height:320px;overflow-y:auto}
#xpkg-panel.open{display:block}
.xpkg-hdr{padding:6px 14px;font-size:11px;font-weight:700;text-transform:uppercase;
          letter-spacing:.5px;color:var(--muted);background:#f0f0f0;
          border-bottom:1px solid var(--border);position:sticky;top:0}
.xpkg-row{padding:7px 14px;cursor:pointer;border-bottom:1px solid var(--border-lt);
          display:flex;align-items:baseline;gap:10px}
.xpkg-row:hover{background:var(--accent-lt)}
.xpkg-key{font-family:"Cascadia Code","Consolas","Courier New",monospace;font-size:12px;
          font-weight:600;color:var(--text);white-space:nowrap}
.xpkg-pkg{font-size:11px;color:var(--muted);flex:1;overflow:hidden;
          text-overflow:ellipsis;white-space:nowrap}
.xpkg-cur{color:var(--accent);font-size:11px;white-space:nowrap}

/* ── Package picker modal ── */
.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:100;
                align-items:flex-start;justify-content:center;padding-top:60px}
.modal-backdrop.open{display:flex}
.modal-card{background:#fff;border-radius:6px;box-shadow:0 8px 32px rgba(0,0,0,.28);
            width:520px;max-height:70vh;display:flex;flex-direction:column;overflow:hidden}
.modal-hdr{padding:12px 16px;border-bottom:1px solid var(--border);font-weight:600;
           display:flex;align-items:center;gap:8px}
.modal-hdr span{flex:1}
.modal-close{background:none;border:none;font-size:18px;cursor:pointer;color:var(--muted);
             line-height:1;padding:2px 6px}
.modal-close:hover{color:var(--text)}
.modal-search{padding:8px 12px;border-bottom:1px solid var(--border)}
.modal-search input{width:100%;padding:6px 10px;border:1px solid #b3b3b3;border-radius:4px;
                    font-size:13px;font-family:inherit;outline:none}
.modal-search input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(0,120,212,.2)}
.modal-list{overflow-y:auto;flex:1}
.modal-item{padding:8px 16px;cursor:pointer;font-size:13px;border-bottom:1px solid var(--border-lt)}
.modal-item:hover{background:var(--accent-lt);color:var(--accent)}
.modal-item.hidden{display:none}
.nav-pkg-btn{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.35);
             padding:5px 12px;border-radius:4px;cursor:pointer;font-size:12px;
             font-family:inherit;transition:background .15s;white-space:nowrap}
.nav-pkg-btn:hover{background:rgba(255,255,255,.28)}
.back-btn{background:rgba(255,255,255,.1);color:#fff;border:1px solid rgba(255,255,255,.25);
          padding:5px 12px;border-radius:4px;cursor:pointer;font-size:12px;
          font-family:inherit;transition:background .15s;white-space:nowrap}
.back-btn:hover{background:rgba(255,255,255,.22)}
</style>
</head>
<body>
<header>
  <h1 id="hdr-title">&#9881; Sublime Text Settings</h1>
  <div class="search-wrap">
    <svg width="14" height="14" viewBox="0 0 16 16" fill="white">
      <path d="M11.742 10.344a6.5 6.5 0 1 0-1.397 1.398h-.001c.03.04.062.078.098.115l3.85 3.85a1 1 0 0 0 1.415-1.414l-3.85-3.85a1.007 1.007 0 0 0-.115-.099zm-5.242 1.656a5.5 5.5 0 1 1 0-11 5.5 5.5 0 0 1 0 11z"/>
    </svg>
    <input type="search" id="search" placeholder="Filter settings&#8230;" oninput="onSearch()">
  </div>
  <div class="spacer"></div>
  <span class="hdr-info" id="hdr-info"></span>
  <button id="back-btn" class="back-btn" style="display:none">&#9664; Preferences</button>
  <button class="nav-pkg-btn" onclick="openPkgModal()">&#128230; Packages</button>
  <button class="close-btn" onclick="closeApp()">&#10005;&nbsp; Close</button>
</header>

<!-- Package picker modal -->
<div class="modal-backdrop" id="pkg-modal" onclick="if(event.target===this)closePkgModal()">
  <div class="modal-card">
    <div class="modal-hdr">
      <span>&#128230; Browse Package Settings</span>
      <button class="modal-close" onclick="closePkgModal()">&#10005;</button>
    </div>
    <div class="modal-search">
      <input type="search" id="pkg-search" placeholder="Filter packages&#8230;" oninput="filterPkgs()">
    </div>
    <div class="modal-list" id="pkg-list"></div>
  </div>
</div>

<div style="position:relative;flex:1;display:flex;flex-direction:column;overflow:hidden">
  <div id="xpkg-panel">
    <div class="xpkg-hdr">All Packages — matching settings</div>
    <div id="xpkg-list"></div>
  </div>
  <div class="body" style="flex:1">
    <nav id="sidebar"></nav>
    <main id="main"></main>
  </div>
</div>

<footer>
  <span id="status-msg">Ready</span>
  <div class="spacer"></div>
  <span class="mod-count" id="mod-count"></span>
</footer>

<script>
const D = __DATA__;

let _modifiedKeys = new Set(Object.keys(D.user_prefs));
let _activeSection = null;

function esc(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function effectiveValue(key) {
  return key in D.user_prefs ? D.user_prefs[key] : D.defaults[key];
}

function buildSidebar() {
  const nav = document.getElementById('sidebar');
  nav.innerHTML = '';
  const cats = ['All', ...Object.keys(D.categories)];
  cats.forEach(cat => {
    const el = document.createElement('div');
    el.className = 'nav-item' + (cat === 'All' ? ' active' : '');
    el.dataset.cat = cat;
    const modCount = cat === 'All'
      ? _modifiedKeys.size
      : (D.categories[cat] || []).filter(k => _modifiedKeys.has(k)).length;
    el.innerHTML = `<span class="nav-label">${esc(cat)}</span>
      <span class="nav-badge${modCount ? ' visible' : ''}">${modCount}</span>`;
    el.onclick = () => selectCat(cat);
    nav.appendChild(el);
  });
}

function buildMain() {
  const main = document.getElementById('main');
  main.innerHTML = '';
  const sections = D.show_order;
  sections.forEach(([section, keys]) => {
    const div = document.createElement('div');
    div.className = 'section';
    div.dataset.section = section;
    div.innerHTML = `<div class="sec-hdr">${esc(section)}</div>
      <table><tbody id="tbody-${CSS.escape(section)}"></tbody></table>`;
    main.appendChild(div);
    const tbody = document.getElementById('tbody-' + CSS.escape(section));
    keys.forEach(key => {
      if (!(key in D.defaults)) return;
      tbody.appendChild(buildRow(key));
    });
  });
}

function buildRow(key) {
  const val = effectiveValue(key);
  const defVal = D.defaults[key];
  const isModified = _modifiedKeys.has(key);
  const desc = D.descriptions[key] || '';
  const hasUsefulDefault = defVal !== null && defVal !== undefined && defVal !== '';
  const tr = document.createElement('tr');
  tr.id = 'row-' + key;
  tr.className = isModified ? 'modified' : '';
  tr.dataset.key = key;
  tr.dataset.desc = desc.toLowerCase();

  let ctrlHtml = '';
  if (key in D.enums && D.enums[key].length) {
    const opts = D.enums[key].map(o =>
      `<option value="${esc(o)}"${String(val)===String(o)?' selected':''}>${esc(o)}</option>`
    ).join('');
    ctrlHtml = `<select onchange="applyChange('${key}',this.value)">${opts}</select>`;
  } else if (typeof defVal === 'boolean') {
    const chk = val ? 'checked' : '';
    ctrlHtml = `<input type="checkbox" ${chk} onchange="applyChange('${key}',this.checked)">`;
  } else if (typeof defVal === 'number') {
    ctrlHtml = `<input type="number" value="${esc(val)}" style="min-width:100px;max-width:120px"
      onchange="applyChange('${key}',parseFloat(this.value)||this.value)"
      onkeydown="if(event.key==='Enter')this.blur()">`;
  } else if (key === 'font_face') {
    const currentFont = String(val) || ((D.effective||{}).font_face) || '';
    const monoFonts = (D.mono_fonts || []).slice();
    if (currentFont && !monoFonts.includes(currentFont)) monoFonts.unshift(currentFont);
    if (monoFonts.length > 0) {
      const opts = monoFonts.map(f =>
        `<option value="${esc(f)}"${f===currentFont?' selected':''}>${esc(f)}</option>`
      ).join('');
      ctrlHtml = `<select id="ctrl-font_face" style="min-width:200px;max-width:300px"
        onchange="applyChange('${key}',this.value)">${opts}</select>`;
    } else {
      ctrlHtml = `<input type="text" value="${esc(currentFont)}" style="min-width:200px;max-width:300px"
        onchange="applyChange('${key}',this.value.trim())"
        onkeydown="if(event.key==='Enter')this.blur()">`;
    }
  } else {
    const display = Array.isArray(val) ? JSON.stringify(val) : String(val);
    ctrlHtml = `<input type="text" value="${esc(display)}"
      onchange="applyChange('${key}',parseTextValue('${key}',this.value))"
      onkeydown="if(event.key==='Enter')this.blur()">`;
  }

  tr.innerHTML = `
    <td class="lbl">
      <span class="key">${esc(key)}</span>
      ${desc ? `<div class="desc" title="${esc(desc)}">${esc(desc)}</div>` : ''}
    </td>
    <td class="ctrl">
      <div class="ctrl-row">
        ${ctrlHtml}
        ${hasUsefulDefault ? `<button class="reset-lnk" title="Restore default value" onclick="resetKey('${key}')">&#8617; default</button>` : ''}
      </div>
    </td>`;
  return tr;
}

function parseTextValue(key, raw) {
  raw = raw.trim();
  try { return JSON.parse(raw); } catch(e) { return raw; }
}

function applyChange(key, value) {
  fetch('/apply', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key, value, settings_fname: D.settings_fname})
  }).then(r => r.json()).then(r => {
    if (r.ok) {
      D.user_prefs[key] = value;
      _modifiedKeys.add(key);
      const tr = document.getElementById('row-' + key);
      if (tr) tr.className = 'modified';
      updateCounts();
      setStatus('Saved ✓', 'ok');
      setTimeout(() => setStatus('Ready', ''), 1800);
    } else {
      setStatus('Error: ' + (r.error || 'unknown'), 'err');
    }
  }).catch(e => setStatus('Network error', 'err'));
}

function resetKey(key) {
  fetch('/reset', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key, settings_fname: D.settings_fname})
  }).then(r => r.json()).then(r => {
    if (r.ok) {
      delete D.user_prefs[key];
      _modifiedKeys.delete(key);
      const tr = document.getElementById('row-' + key);
      if (tr) tr.parentNode.replaceChild(buildRow(key), tr);
      updateCounts();
      setStatus('Reset to default ✓', 'ok');
      setTimeout(() => setStatus('Ready', ''), 1800);
    } else {
      setStatus('Error: ' + (r.error || 'unknown'), 'err');
    }
  }).catch(e => setStatus('Network error', 'err'));
}

function selectCat(cat) {
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.cat === cat);
  });
  _activeSection = cat;
  if (cat === 'All') {
    document.querySelectorAll('.section').forEach(s => s.style.display = '');
  } else {
    document.querySelectorAll('.section').forEach(s => {
      s.style.display = s.dataset.section === cat ? '' : 'none';
    });
    const sec = document.querySelector(`.section[data-section="${CSS.escape(cat)}"]`);
    if (sec) sec.scrollIntoView({behavior:'smooth', block:'start'});
  }
}

function onSearch() {
  const q = document.getElementById('search').value.toLowerCase().trim();
  let visible = 0;
  document.querySelectorAll('tr[data-key]').forEach(tr => {
    const match = !q || tr.dataset.key.includes(q) || tr.dataset.desc.includes(q);
    tr.classList.toggle('hidden', !match);
    if (match) visible++;
  });
  document.querySelectorAll('.section').forEach(sec => {
    const any = sec.querySelectorAll('tr[data-key]:not(.hidden)').length > 0;
    sec.style.display = any ? '' : 'none';
  });
  if (q) {
    document.querySelectorAll('.section').forEach(s => {
      if (s.querySelectorAll('tr[data-key]:not(.hidden)').length) s.style.display = '';
    });
  }
  updateCounts();
  updateXpkg(q);
}

function updateXpkg(q) {
  const panel = document.getElementById('xpkg-panel');
  const list  = document.getElementById('xpkg-list');
  if (!q) { panel.classList.remove('open'); return; }
  const cur = D.settings_resource;
  const hits = (D.all_keys || []).filter(r => r.key.toLowerCase().includes(q));
  if (!hits.length) { panel.classList.remove('open'); return; }
  list.innerHTML = '';
  hits.slice(0, 80).forEach(r => {
    const row = document.createElement('div');
    row.className = 'xpkg-row';
    const isCur = r.resource === cur;
    row.innerHTML = `<span class="xpkg-key">${esc(r.key)}</span>
      <span class="xpkg-pkg">${esc(r.pkg)}</span>
      ${isCur ? '<span class="xpkg-cur">current</span>' : ''}`;
    row.onclick = () => {
      document.getElementById('search').value = '';
      panel.classList.remove('open');
      if (isCur) {
        const tr = document.getElementById('row-' + r.key);
        if (tr) tr.scrollIntoView({behavior:'smooth', block:'center'});
      } else {
        navigateTo(r.resource + '#' + r.key);
      }
    };
    list.appendChild(row);
  });
  panel.classList.add('open');
}

function updateCounts() {
  const total = Object.keys(D.defaults).length;
  const mod = _modifiedKeys.size;
  document.getElementById('mod-count').textContent =
    mod ? `${mod} of ${total} modified` : `${total} settings`;
  document.getElementById('hdr-info').textContent =
    mod ? `${mod} modified` : '';
  document.querySelectorAll('.nav-item').forEach(el => {
    const cat = el.dataset.cat;
    const keys = cat === 'All' ? Object.keys(D.defaults) : (D.categories[cat] || []);
    const count = keys.filter(k => _modifiedKeys.has(k)).length;
    const badge = el.querySelector('.nav-badge');
    badge.textContent = count;
    badge.classList.toggle('visible', count > 0);
  });
}

function setStatus(msg, cls) {
  const el = document.getElementById('status-msg');
  el.textContent = msg;
  el.className = cls;
}

function closeApp() {
  fetch('/close').finally(() => window.close());
}

function navigateTo(resourceAndKey) {
  if (resourceAndKey === '__PREFS__') resourceAndKey = D.prefs_resource;
  const hashIdx = resourceAndKey.indexOf('#');
  const resource = hashIdx >= 0 ? resourceAndKey.slice(0, hashIdx) : resourceAndKey;
  const focusKey = hashIdx >= 0 ? resourceAndKey.slice(hashIdx + 1) : '';
  if (focusKey) sessionStorage.setItem('focusKey', focusKey);
  setStatus('Loading…', '');
  fetch('/navigate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({resource})
  }).catch(() => {});
}

function openPkgModal() {
  const list = document.getElementById('pkg-list');
  list.innerHTML = '';
  (D.package_list || []).forEach(pkg => {
    const el = document.createElement('div');
    el.className = 'modal-item';
    el.dataset.label = pkg.label.toLowerCase();
    el.textContent = pkg.label;
    el.onclick = () => { closePkgModal(); navigateTo(pkg.resource); };
    list.appendChild(el);
  });
  document.getElementById('pkg-search').value = '';
  document.getElementById('pkg-modal').classList.add('open');
  document.getElementById('pkg-search').focus();
}

function closePkgModal() {
  document.getElementById('pkg-modal').classList.remove('open');
}

function filterPkgs() {
  const q = document.getElementById('pkg-search').value.toLowerCase();
  document.querySelectorAll('.modal-item').forEach(el => {
    el.classList.toggle('hidden', q && !el.dataset.label.includes(q));
  });
}

// Poll for data updates and auto-reload
(function() {
  const gen = D.gen;
  setInterval(function() {
    fetch('/ping').then(r => r.json()).then(d => {
      if (d.gen !== gen) window.location.reload();
    }).catch(() => {});
  }, 2000);
})();

// Init
document.getElementById('hdr-title').textContent = '⚙ ' + (D.package_label || 'Sublime Text Settings');
document.title = D.package_label || 'Sublime Text Settings';
if (!D.is_prefs) {
  const backBtn = document.getElementById('back-btn');
  backBtn.style.display = '';
  backBtn.onclick = () => navigateTo('__PREFS__');
}
buildSidebar();
buildMain();
updateCounts();

// Scroll to key if navigated here from a search result
(function() {
  const key = sessionStorage.getItem('focusKey');
  if (!key) return;
  sessionStorage.removeItem('focusKey');
  setTimeout(() => {
    const tr = document.getElementById('row-' + key);
    if (tr) {
      tr.scrollIntoView({behavior:'smooth', block:'center'});
      tr.style.outline = '2px solid var(--accent)';
      setTimeout(() => tr.style.outline = '', 2000);
    }
  }, 100);
})();
</script>
</body>
</html>
"""


# ── HTTP server ───────────────────────────────────────────────────────────────

_data = {}
_gen  = 0
_server = None
_port   = None
_lock   = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, body, status=200, ct="text/html; charset=utf-8"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj), status, "application/json")

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n))

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with _lock:
                page = HTML.replace("__DATA__", json.dumps(_data))
            self._send(page)
        elif path == "/ping":
            with _lock:
                gen = _data.get("gen", 0)
            self._json({"gen": gen})
        elif path == "/close":
            self._send("ok")
        else:
            self._send("Not found", 404)

    def do_POST(self):
        try:
            body = self._read_json()
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 400)
            return

        path = self.path.split("?")[0]

        if path == "/apply":
            key   = body.get("key", "")
            value = body.get("value")
            fname = body.get("settings_fname", "Preferences.sublime-settings")
            def apply():
                try:
                    s = sublime.load_settings(fname)
                    s.set(key, value)
                    sublime.save_settings(fname)
                except Exception as e:
                    print(f"st_settings apply error: {e}")
            sublime.set_timeout(apply, 0)
            self._json({"ok": True})

        elif path == "/reset":
            key   = body.get("key", "")
            fname = body.get("settings_fname", "Preferences.sublime-settings")
            def reset():
                try:
                    s = sublime.load_settings(fname)
                    s.erase(key)
                    sublime.save_settings(fname)
                except Exception as e:
                    print(f"st_settings reset error: {e}")
            sublime.set_timeout(reset, 0)
            self._json({"ok": True})

        elif path == "/navigate":
            resource = body.get("resource", PREFS_RESOURCE)
            threading.Thread(target=_update_data, args=(resource,), daemon=True).start()
            self._json({"ok": True})

        else:
            self._json({"ok": False, "error": "unknown"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


class _Server(HTTPServer):
    allow_reuse_address = True


_FIXED_PORT = 57321


def _ensure_server():
    global _server, _port
    if _server is not None:
        return
    _server = _Server(("127.0.0.1", _FIXED_PORT), _Handler)
    _port = _FIXED_PORT
    t = threading.Thread(target=_server.serve_forever, daemon=True)
    t.start()


def _update_data(settings_resource):
    global _gen
    data = _build_data(settings_resource)
    with _lock:
        _gen += 1
        data["gen"] = _gen
        _data.clear()
        _data.update(data)


def _open(settings_resource=PREFS_RESOURCE):
    import webbrowser
    _ensure_server()          # binds port synchronously
    _update_data(settings_resource)
    webbrowser.open(f"http://127.0.0.1:{_port}")
    sublime.status_message(f"ST Settings: http://127.0.0.1:{_port}")


# ── ST lifecycle ──────────────────────────────────────────────────────────────

def plugin_unloaded():
    global _server
    if _server:
        _server.shutdown()
        _server.server_close()
        _server = None


# ── commands ──────────────────────────────────────────────────────────────────

class StSettingsOpenCommand(sublime_plugin.WindowCommand):
    def run(self):
        threading.Thread(target=_open, daemon=True).start()
