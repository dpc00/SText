"""Sublime Settings editor (v2) — Eclipse Property Sheet style.

Left pane: a directory-style tree of categories -> settings (folded by
default, expand on click). Right pane: an Eclipse-style Property|Value sheet
for the ONE selected setting, showing every dimension (name, type, enum,
default, effective [from the active view], owner, the full override chain
Default -> Platform -> Distraction Free -> User with the winner marked) and
every consequence (which keybindings/menus/plugin code read it, what breaks if
it changes). The editable Value row hosts a typed cell editor; a Write-to
dropdown picks the destination settings file (default = current source); a
Restore Default action reverts; the description/help area sits at the bottom
(Eclipse convention); and edit-time consequence warnings surface inline and in
a confirm dialog before the write. A Stop button kills the server.

Architecture (see config/EDITOR_DESIGN.md):
- In-ST Python HTTP server + browser UI (port 57323).
- Defaults from Default/Preferences.sublime-settings + platform variant.
- Descriptions parsed from the // comment blocks in Default/Preferences.
- Effective value from the active view's merged settings (all layers).
- Override chain + owner from find_resources/decode_value.
- Reads from .sublime-keymap/.sublime-menu contexts + a loose-Packages .py grep.
- Writes byte-faithful via vendored json5 (ModelLoader positions) + position
  surgery so comments, trailing commas, spacing and line endings on UNEDITED
  lines are preserved. Writes generalize to any User/*.sublime-settings target.
"""
import os
import re
import sys
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- vendor json5 (pure-Python, bundled at config/lib/json5) -----------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(_HERE, "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import json5  # noqa: E402
from json5.loader import loads as _j5loads, ModelLoader as _ModelLoader  # noqa: E402

import sublime
import sublime_plugin  # noqa: E402

_PORT = 57324  # was 57323; bumped to escape a zombie server left by an earlier
               # reload (reload_plugin doesn't kill the old server thread, and the
               # orphan held the port). Revert to 57323 after a clean ST restart.
_SERVER = [None]
_gen = [0]
_MISSING = object()

# --- known enum settings (curated; others get free-text) ---------------------
_ENUMS = {
    "word_wrap": ["auto", "true", "false"],
    "draw_white_space": ["none", "selection", "all"],
    "draw_minimap_border": ["auto", "true", "false"],
    "trim_trailing_white_space_on_save": ["none", "all", "modified"],
    "ensure_newline_at_eof_on_save": ["auto", "true", "false"],
    "default_line_ending": ["system", "unix", "windows"],
    "caret_style": ["solid", "blink", "smooth", "phase", "wide"],
    "fold_style": ["auto", "classic", "indent"],
    "control_character_style": ["hex", "none", "name"],
    "wrap_width_style": ["constant", "variable"],
    "auto_complete_preserve_order": ["some", "none", "always"],
    "show_definitions": ["auto", "true", "false"],
    "highlight_line": ["none", "gutter", "line", "all"],
    "tab_completion": ["true", "false", "insert"],
    "shift_tab_unindent": ["auto", "true", "false"],
    "drag_text": ["true", "false", "single"],
}

# Short curated descriptions used to fill gaps the comment parser misses.
_DESC_OVERLAY = {
    "ignored_packages": "Packages disabled at startup (e.g. Vintage). Changing this needs a restart.",
    "installed_packages": "Packages Package Control should keep installed.",
    "folder_exclude_patterns": "Glob patterns of folders hidden from the sidebar and excluded from indexing.",
    "file_exclude_patterns": "Glob patterns of files hidden from the sidebar and excluded from indexing.",
}

# --- categories --------------------------------------------------------------
_CATEGORY_ORDER = [
    "Appearance & Theme", "Font", "Tabs & Indentation", "Wrapping & Lines",
    "Whitespace", "Gutter & Rulers", "Sidebar & Minimap", "Tabs Bar & Menu",
    "Find & Replace", "Auto-complete & Snippets", "Spell Check", "File & Save",
    "Indexing & Goto", "Application Behavior", "Packages", "Behavior & Selection",
    "Other",
]
_RULES = [
    ("Appearance & Theme", ["color_scheme", "theme", "mini_diff", "overlay_scroll_bars", "highlight_line", "line_numbers", "match_brackets"]),
    ("Font", ["font", "glyph_size"]),
    ("Tabs & Indentation", ["tab_size", "translate_tabs_to_spaces", "use_tab_stops", "detect_indentation", "auto_indent", "smart_indent", "indent_to_bracket", "trim_automatic_white_space", "indent_guide_options", "shift_tab_unindent", "use_nested_indent"]),
    ("Wrapping & Lines", ["word_wrap", "wrap_width", "line_padding", "default_line_ending", "ensure_newline_at_eof", "line_numbers"]),
    ("Whitespace", ["draw_white_space", "draw_white_space_selection", "trailing_white_space", "fade_fold_buttons", "draw_indent_guides", "draw_unloaded_tabs"]),
    ("Gutter & Rulers", ["gutter", "margin", "ruler", "fold_"]),
    ("Sidebar & Minimap", ["sidebar", "minimap", "tree_animation", "always_show_minimap_viewport", "show_open_files"]),
    ("Tabs Bar & Menu", ["show_tab_bar", "tab_bar", "hide_menu", "show_sidebar", "show_status_bar", "auto_hide_menu", "auto_hide_tabs", "remember_tab_switch"]),
    ("Find & Replace", ["find", "replace", "incremental", "auto_hide_find", "highlight_find_results"]),
    ("Auto-complete & Snippets", ["auto_complete", "snippet", "completion", "auto_close", "auto_match", "tab_completion"]),
    ("Spell Check", ["spell"]),
    ("File & Save", ["file", "save", "reload", "prompt_delete", "create_file", "open_files", "close_windows", "remember_open_files", "always_prompt_for_file_reload"]),
    ("Indexing & Goto", ["index", "goto", "preview_file", "reveal", "show_definitions", "gpu_indexing"]),
    ("Application Behavior", ["hot_exit", "remember_full_screen", "animation", "scroll_past", "gpu", "hardware_accel", "close_windows_when_empty"]),
    ("Packages", ["ignored_packages", "installed_packages", "package"]),
    ("Behavior & Selection", ["caret", "selection", "bracket", "match", "draw_minimap", "scroll", "mouse", "drag", "copy", "paste", "drag_text"]),
]


def _categorize(name):
    nl = name.lower()
    for cat, kws in _RULES:
        for kw in kws:
            if kw in nl:
                return cat
    return "Other"


# --- paths -------------------------------------------------------------------
def _user_dir():
    return os.path.join(sublime.packages_path(), "User")


def _settings_path(rel):
    return os.path.join(_user_dir(), rel.replace("/", os.sep))


def _platform_name():
    p = sublime.platform()
    return "Windows" if p == "windows" else ("OSX" if p == "osx" else "Linux")


def _line_ending(text):
    if "\r\n" in text:
        return "\r\n"
    if "\n" in text:
        return "\n"
    return "\r\n" if sublime.platform() == "windows" else "\n"


# --- active view / syntax ----------------------------------------------------
def _active_view():
    w = sublime.active_window()
    return w.active_view() if w else None


def _active_syntax_name():
    v = _active_view()
    if not v:
        return None
    try:
        syn = v.syntax()
        return syn.name if syn else None
    except Exception:
        return None


def _current_source_rel():
    """The settings file that applies to the active view, by precedence:
    an open *.sublime-settings tab in User/ > syntax-specific > Preferences."""
    v = _active_view()
    if v:
        fn = v.file_name()
        if fn and fn.endswith(".sublime-settings"):
            d = os.path.dirname(fn)
            if os.path.normpath(d) == os.path.normpath(_user_dir()):
                return os.path.basename(fn)
    syn = _active_syntax_name()
    if syn:
        return syn + ".sublime-settings"
    return "Preferences.sublime-settings"


def _write_targets():
    cur = _current_source_rel()
    syn = _active_syntax_name()
    syn_rel = (syn + ".sublime-settings") if syn else None
    order = []
    if cur:
        order.append(cur)
    if syn_rel and syn_rel != cur:
        order.append(syn_rel)
    for r in ("Preferences.sublime-settings", "Preferences (Distraction Free).sublime-settings"):
        if r not in order:
            order.append(r)
    out = []
    for r in order:
        out.append({"rel": r, "label": r, "exists": os.path.exists(_settings_path(r))})
    return {"targets": out, "current_source": cur}


# --- resource decode cache ---------------------------------------------------
_RES_CACHE = {}


def _res_decode(res):
    if res not in _RES_CACHE:
        try:
            _RES_CACHE[res] = sublime.decode_value(sublime.load_resource(res))
        except Exception:
            _RES_CACHE[res] = None
    return _RES_CACHE[res]


def _default_res(filename):
    for r in sublime.find_resources(filename):
        if r.startswith("Packages/Default/"):
            return r
    return None


def _load_defaults():
    merged = {}
    plat = _platform_name()
    for fn in ("Preferences.sublime-settings", "Preferences (%s).sublime-settings" % plat):
        r = _default_res(fn)
        if r:
            d = _res_decode(r)
            if isinstance(d, dict):
                merged.update(d)
    return merged


_DESCRIPTIONS = None


def _parse_default_descriptions():
    """Parse // comment blocks in Default/Preferences.sublime-settings.
    Each blank-line-delimited block: trailing 'key': value line owns the
    preceding // comment lines as its description."""
    global _DESCRIPTIONS
    if _DESCRIPTIONS is not None:
        return _DESCRIPTIONS
    out = {}
    r = _default_res("Preferences.sublime-settings")
    if r:
        try:
            text = sublime.load_resource(r)
        except Exception:
            text = ""
        for block in re.split(r"\n\s*\n", text):
            comments = []
            key = None
            for ln in block.splitlines():
                s = ln.strip()
                if s.startswith("//"):
                    comments.append(s.lstrip("/").strip())
                elif key is None and s and not s.startswith("/*") and not s.startswith("*"):
                    m = re.match(r'"([^"]+)"\s*:', s)
                    if m:
                        key = m.group(1)
            if key:
                txt = " ".join([c for c in comments if c]).strip()
                if txt:
                    out[key] = txt
    _DESCRIPTIONS = out
    return out


def _desc_for(name):
    d = _parse_default_descriptions().get(name)
    if d:
        return d
    return _DESC_OVERLAY.get(name, "")


# --- user file values (User/Preferences) -------------------------------------
def _read_file(p):
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8", newline="") as f:
                return f.read()
        except Exception:
            return "{}"
    return "{}"


def _write_file(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    _gen[0] += 1


def _user_prefs_path():
    return _settings_path("Preferences.sublime-settings")


def _user_values():
    try:
        d = json5.loads(_read_file(_user_prefs_path()))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _target_values(rel):
    try:
        d = json5.loads(_read_file(_settings_path(rel)))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# --- types / effective -------------------------------------------------------
def _infer_type(name, value):
    if name in _ENUMS and _ENUMS[name] is not None:
        return "enum"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _effective(name, default_val):
    v = _active_view()
    if v:
        try:
            val = v.settings().get(name, _MISSING)
            if val is not _MISSING:
                return val
        except Exception:
            pass
    uv = _user_values().get(name, _MISSING)
    if uv is not _MISSING:
        return uv
    return default_val


def _same_json(a, b):
    try:
        return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    except Exception:
        return a == b


# --- owner index -------------------------------------------------------------
_OWNER = None


def _owner_index():
    global _OWNER
    if _OWNER is not None:
        return _OWNER
    idx = {}
    for res in sublime.find_resources("*.sublime-settings"):
        try:
            d = sublime.decode_value(sublime.load_resource(res))
        except Exception:
            d = None
        if not isinstance(d, dict):
            continue
        parts = res.split("/")
        pkg = parts[1] if len(parts) > 2 else "?"
        for k in d.keys():
            idx.setdefault(k, []).append({"res": res, "pkg": pkg})
    _OWNER = idx
    return idx


def _owner_for(name):
    entries = _owner_index().get(name, [])
    if not entries:
        return "core (no resource declares it)"
    pkgs = sorted(set(e["pkg"] for e in entries))
    if pkgs == ["Default"]:
        return "core"
    nondefault = [p for p in pkgs if p != "Default"]
    if nondefault:
        return "package: " + ", ".join(nondefault)
    return "core"


# --- override chain ----------------------------------------------------------
def _override_chain(name):
    plat = _platform_name()
    chain = []
    layers = [
        ("Default", "Preferences.sublime-settings"),
        ("Platform (%s)" % plat, "Preferences (%s).sublime-settings" % plat),
        ("Distraction Free", "Preferences (Distraction Free).sublime-settings"),
    ]
    for label, fn in layers:
        r = _default_res(fn)
        if r:
            d = _res_decode(r)
            if isinstance(d, dict) and name in d:
                chain.append({"layer": label, "value": d[name], "source": "Default/" + fn})
    uv = _user_values().get(name, _MISSING)
    if uv is not _MISSING:
        chain.append({"layer": "User", "value": uv, "source": "User/Preferences.sublime-settings"})
    # mark winner (last wins)
    for i, e in enumerate(chain):
        e["wins"] = (i == len(chain) - 1)
    return chain


# --- reads: keymap / menu / plugin code --------------------------------------
_KEYMAP_IDX = None


def _keymap_index():
    global _KEYMAP_IDX
    if _KEYMAP_IDX is not None:
        return _KEYMAP_IDX
    idx = {}
    for res in sublime.find_resources("*.sublime-keymap"):
        try:
            d = sublime.decode_value(sublime.load_resource(res))
        except Exception:
            continue
        if not isinstance(d, list):
            continue
        for entry in d:
            if not isinstance(entry, dict):
                continue
            ctx = entry.get("context")
            if not isinstance(ctx, list):
                continue
            for c in ctx:
                if not isinstance(c, dict):
                    continue
                k = c.get("key")
                if isinstance(k, str) and k.startswith("setting."):
                    nm = k[len("setting."):]
                    idx.setdefault(nm, []).append({
                        "keys": entry.get("keys"),
                        "command": entry.get("command"),
                        "file": res,
                        "operator": c.get("operator"),
                        "operand": c.get("operand"),
                    })
    _KEYMAP_IDX = idx
    return idx


def _menu_reads(name):
    hits = []
    for res in sublime.find_resources("*.sublime-menu"):
        try:
            d = sublime.decode_value(sublime.load_resource(res))
        except Exception:
            continue
        stack = [d]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                ctx = node.get("context")
                if isinstance(ctx, list):
                    for c in ctx:
                        if isinstance(c, dict) and c.get("key") == "setting." + name:
                            hits.append({
                                "caption": node.get("caption"),
                                "command": node.get("command"),
                                "file": res,
                                "operator": c.get("operator"),
                                "operand": c.get("operand"),
                            })
                for v in node.values():
                    if isinstance(v, (list, dict)):
                        stack.append(v)
    return hits


_PY_CACHE = None


def _py_cache():
    global _PY_CACHE
    if _PY_CACHE is not None:
        return _PY_CACHE
    cache = {}
    root = sublime.packages_path()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                sz = os.path.getsize(p)
            except Exception:
                continue
            if sz > 200_000:
                continue
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    cache[p] = f.read()
            except Exception:
                pass
    _PY_CACHE = cache
    return cache


def _py_reads(name):
    cache = _py_cache()
    pats = [
        re.compile(r'\.get\(\s*["\']' + re.escape(name) + r'["\']'),
        re.compile(r'\[\s*["\']' + re.escape(name) + r'["\']\s*\]'),
    ]
    hits = []
    for p, txt in cache.items():
        for ln, line in enumerate(txt.splitlines(), 1):
            for pat in pats:
                if pat.search(line):
                    rel = os.path.relpath(p, sublime.packages_path()).replace("\\", "/")
                    hits.append({"file": rel, "line": ln, "snippet": line.strip()[:140]})
                    break
        if len(hits) >= 60:
            break
    return hits


# --- consequence warnings ----------------------------------------------------
_SAVE_EFFECT = {
    "trim_trailing_white_space_on_save": "files are rewritten on save (trailing whitespace stripped)",
    "ensure_newline_at_eof_on_save": "files are rewritten on save (final newline added/ensured)",
    "default_line_ending": "files are rewritten on save (line endings converted)",
}


def _warnings(name, new_value, usage):
    w = []
    kb = usage.get("keybindings") or []
    if kb:
        cmds = sorted(set((h.get("command") or "?") for h in kb))
        w.append("Tested by %d keybinding(s); changing it may stop them matching: %s" % (len(kb), ", ".join(cmds[:8])))
    if name == "ignored_packages" and isinstance(new_value, list):
        w.append("Packages added/removed here are enabled/disabled at startup (needs restart); their commands/keybindings/menus change.")
    if name in _SAVE_EFFECT and new_value and new_value != "none":
        w.append("With this value, %s." % _SAVE_EFFECT[name])
    if name == "index_files" and not new_value:
        w.append("Disabling indexing degrades auto-complete, Goto Definition, and project-wide search.")
    if name in _ENUMS and new_value not in _ENUMS[name]:
        w.append("Value %r is not a recognized option (allowed: %s)." % (new_value, ", ".join(_ENUMS[name])))
    return w


# --- comment-preserving write path (position-based text surgery) ------------
def _pos(text, lineno, col):
    s = 0
    for _ in range(lineno - 1):
        s = text.index("\n", s) + 1
    return s + col


def _kvp_map(text):
    try:
        m = _j5loads(text, loader=_ModelLoader())
        if m and m.value and hasattr(m.value, "key_value_pairs"):
            return {k.key.characters: k for k in m.value.key_value_pairs}
    except Exception:
        pass
    return {}


def _indent_of(text):
    kvps = _kvp_map(text)
    if not kvps:
        return "    "
    m = _j5loads(text, loader=_ModelLoader())
    k0 = m.value.key_value_pairs[0]
    ls = _pos(text, k0.key.lineno, 0)
    return text[ls:_pos(text, k0.key.lineno, k0.key.col_offset)]


def _set_existing(text, name, value):
    k = _kvp_map(text)[name].value
    s = _pos(text, k.lineno, k.col_offset)
    e = _pos(text, k.end_lineno, k.end_col_offset)
    return text[:s] + json.dumps(value) + text[e:]


def _delete(text, name):
    k = _kvp_map(text)[name]
    line_start = _pos(text, k.key.lineno, 0)
    val_end = _pos(text, k.value.end_lineno, k.value.end_col_offset)
    ci = text.find(",", val_end)
    nl_after = text.find("\n", val_end)
    if ci != -1 and (nl_after == -1 or ci < nl_after):
        # a trailing comma follows the value on the same line: drop through its
        # newline so the whole key line disappears.
        end_nl = text.find("\n", ci)
        end = (end_nl + 1) if end_nl != -1 else (ci + 1)
    else:
        # last entry (no trailing comma): drop this whole line + its newline.
        end = (nl_after + 1) if nl_after != -1 else len(text)
    return text[:line_start] + text[end:]


def _add(text, name, value):
    nl = _line_ending(text)
    kvps = _kvp_map(text)
    if not kvps:
        return "{" + nl + _indent_of(text) + json.dumps(name) + ": " + json.dumps(value) + nl + "}"
    m = _j5loads(text, loader=_ModelLoader())
    last = m.value.key_value_pairs[-1].value
    s = _pos(text, last.end_lineno, last.end_col_offset)
    indent = _indent_of(text)
    return text[:s] + "," + nl + indent + json.dumps(name) + ": " + json.dumps(value) + text[s:]


def _apply_set(name, value, target_rel):
    p = _settings_path(target_rel)
    text = _read_file(p)
    if name in _kvp_map(text):
        text = _set_existing(text, name, value)
    else:
        text = _add(text, name, value)
    _write_file(p, text)


def _apply_delete(name, target_rel):
    p = _settings_path(target_rel)
    text = _read_file(p)
    if name in _kvp_map(text):
        text = _delete(text, name)
        _write_file(p, text)


# --- catalog + detail --------------------------------------------------------
def _build_catalog():
    defaults = _load_defaults()
    user = _user_values()
    kidx = _keymap_index()
    names = sorted(set(defaults) | set(user))
    out = []
    for name in names:
        dv = defaults.get(name, None)
        eff = _effective(name, dv)
        overridden = not _same_json(eff, dv)
        out.append({
            "name": name,
            "category": _categorize(name),
            "type": _infer_type(name, eff),
            "default": dv,
            "effective": eff,
            "overridden": overridden,
            "enum": _ENUMS.get(name),
            "has_usage": name in kidx,
        })
    cats = [c for c in _CATEGORY_ORDER if any(s["category"] == c for s in out)]
    cats += sorted(set(s["category"] for s in out) - set(cats))
    wt = _write_targets()
    return {
        "settings": out,
        "categories": cats,
        "write_targets": wt["targets"],
        "current_source": wt["current_source"],
        "gen": _gen[0],
    }


def _detail(name):
    defaults = _load_defaults()
    dv = defaults.get(name, None)
    eff = _effective(name, dv)
    kidx = _keymap_index()
    kb = kidx.get(name, [])
    usage = {
        "keybindings": kb,
        "menus": _menu_reads(name),
        "plugins": _py_reads(name),
    }
    wt = _write_targets()
    return {
        "name": name,
        "category": _categorize(name),
        "type": _infer_type(name, eff),
        "default": dv,
        "effective": eff,
        "owner": _owner_for(name),
        "override_chain": _override_chain(name),
        "enum": _ENUMS.get(name),
        "desc": _desc_for(name),
        "usage": usage,
        "overridden": not _same_json(eff, dv),
        "write_targets": wt["targets"],
        "current_source": wt["current_source"],
        "gen": _gen[0],
    }


# --- keybindings: runtime command registry -----------------------------------
_CMD = None


def _runtime_commands():
    global _CMD
    if _CMD is not None:
        return _CMD
    out = {}
    for lst in (sublime_plugin.application_command_classes,
                sublime_plugin.window_command_classes,
                sublime_plugin.text_command_classes):
        for c in lst or []:
            try:
                n = c.__new__(c).name()
            except Exception:
                continue
            if isinstance(n, str) and n:
                out[n] = (c.__module__ or "?") + "." + c.__name__
    _CMD = out
    return out


# Known commands = Python runtime classes + everything Default package binds
# (Default only references real commands, so this captures the C++ builtins
# that Python introspection cannot see — exit, new_window, copy, save, ...).
_DEFAULT_CMD = None


def _default_resource_commands():
    global _DEFAULT_CMD
    if _DEFAULT_CMD is not None:
        return _DEFAULT_CMD
    out = set()
    res = (sublime.find_resources("*.sublime-keymap")
           + sublime.find_resources("*.sublime-menu")
           + sublime.find_resources("*.sublime-commands"))
    for r in res:
        if not r.startswith("Packages/Default/"):
            continue
        try:
            d = sublime.decode_value(sublime.load_resource(r))
        except Exception:
            continue
        stack = [d]
        while stack:
            n = stack.pop()
            if isinstance(n, list):
                stack.extend(n)
            elif isinstance(n, dict):
                c = n.get("command")
                if isinstance(c, str):
                    out.add(c)
                for v in n.values():
                    if isinstance(v, (list, dict)):
                        stack.append(v)
    _DEFAULT_CMD = out
    return out


def _known_commands():
    return set(_runtime_commands().keys()) | _default_resource_commands()


# --- keybindings: catalog (merged Default + platform + User) ------------------
_KM = None
_KM_GEN = -1


def _km_real_path(res):
    """Packages/User/Foo.sublime-keymap -> fs path; package files -> None (RO)."""
    parts = res.split("/")
    if len(parts) >= 2 and parts[1] == "User":
        return os.path.join(sublime.packages_path(), "User", *parts[2:])
    return None


def _km_chord(keys):
    if isinstance(keys, list):
        return " ".join(str(k) for k in keys)
    return str(keys) if keys else ""


def _keymap_catalog():
    global _KM, _KM_GEN
    if _KM is not None and _KM_GEN == _gen[0]:
        return _KM
    known = _known_commands()
    recs = []
    chord_idx = {}
    for res in sublime.find_resources("*.sublime-keymap"):
        try:
            d = sublime.decode_value(sublime.load_resource(res))
        except Exception:
            continue
        if not isinstance(d, list):
            continue
        is_user = res.startswith("Packages/User/")
        pkg = res.split("/")[1] if len(res.split("/")) > 2 else "?"
        for li, entry in enumerate(d):
            if not isinstance(entry, dict):
                continue
            keys = entry.get("keys")
            cmd = entry.get("command")
            chord = _km_chord(keys)
            rec = {
                "idx": len(recs),
                "local_index": li,
                "source": res,
                "pkg": pkg,
                "is_user": is_user,
                "writable": is_user,
                "keys": keys,
                "chord": chord,
                "command": cmd,
                "args": entry.get("args"),
                "context": entry.get("context"),
                "dead": isinstance(cmd, str) and cmd not in known,
            }
            recs.append(rec)
            chord_idx.setdefault(chord, []).append(rec["idx"])
    for r in recs:
        group = chord_idx.get(r["chord"], [])
        r["conflict_count"] = max(0, len(group) - 1)
    _KM = {"bindings": recs, "write_targets": _km_write_targets(), "gen": _gen[0]}
    _KM_GEN = _gen[0]
    return _KM


def _keymap_binding(idx):
    cat = _keymap_catalog()
    recs = cat["bindings"]
    if idx < 0 or idx >= len(recs):
        return {"error": "no such binding"}
    r = recs[idx]
    cmds = _runtime_commands()
    conflicts = []
    for other in recs:
        if other["idx"] == idx or other["chord"] != r["chord"] or not r["chord"]:
            continue
        conflicts.append({
            "idx": other["idx"],
            "keys": other["keys"],
            "command": other["command"],
            "context": other["context"],
            "source": other["source"],
            "is_user": other["is_user"],
        })
    return {
        "idx": idx,
        "keys": r["keys"],
        "chord": r["chord"],
        "command": r["command"],
        "args": r["args"],
        "context": r["context"],
        "source": r["source"],
        "is_user": r["is_user"],
        "writable": r["writable"],
        "dead": r["dead"],
        "command_source": cmds.get(r["command"]) if isinstance(r["command"], str) else None,
        "conflicts": conflicts,
        "write_targets": _km_write_targets(),
        "gen": _gen[0],
    }


# --- keybindings: comment-preserving writes (array-of-objects) ---------------
def _km_model(text):
    return _j5loads(text, loader=_ModelLoader())


def _km_entry_text(text, local_index):
    m = _km_model(text)
    el = m.value.values[local_index]
    s = _pos(text, el.lineno, el.col_offset)
    e = _pos(text, el.end_lineno, el.end_col_offset)
    return el, m, s, e


def _km_indent_for(text, m):
    vals = m.value.values
    if not vals:
        return "\t"
    e0 = vals[0]  # the element object; its col_offset is the '{' position
    ls = _pos(text, e0.lineno, 0)
    return text[ls:_pos(text, e0.lineno, e0.col_offset)]


def _km_set_entry(res, local_index, entry, expect_cmd):
    p = _km_real_path(res)
    if not p:
        raise Exception("source is inside a package zip — not writable; add an override in User")
    text = _read_file(p)
    el, m, s, e = _km_entry_text(text, local_index)
    kvp = {kp.key.characters: kp for kp in el.key_value_pairs}
    if expect_cmd is not None:
        cur = kvp.get("command")
        cur_chars = cur.value.characters if cur and hasattr(cur.value, "characters") else None
        if cur_chars != expect_cmd:
            raise Exception("file changed since load — refresh and retry")
    text = text[:s] + json.dumps(entry) + text[e:]
    _write_file(p, text)


def _km_delete_entry(res, local_index, expect_cmd):
    p = _km_real_path(res)
    if not p:
        raise Exception("source is inside a package zip — not writable")
    text = _read_file(p)
    el, m, s_line, _ = _km_entry_text(text, local_index)
    kvp = {kp.key.characters: kp for kp in el.key_value_pairs}
    if expect_cmd is not None:
        cur = kvp.get("command")
        cur_chars = cur.value.characters if cur and hasattr(cur.value, "characters") else None
        if cur_chars != expect_cmd:
            raise Exception("file changed since load — refresh and retry")
    line_start = _pos(text, el.lineno, 0)
    val_end = _pos(text, el.end_lineno, el.end_col_offset)
    ci = text.find(",", val_end)
    nl_after = text.find("\n", val_end)
    if ci != -1 and (nl_after == -1 or ci < nl_after):
        # this entry has a trailing comma: drop the entry line + that comma
        end_nl = text.find("\n", ci)
        end = (end_nl + 1) if end_nl != -1 else (ci + 1)
        text = text[:line_start] + text[end:]
    else:
        # last entry (no trailing comma): drop its line, then strip the now-
        # dangling comma left on the previous entry's line by the separator.
        end = (nl_after + 1) if nl_after != -1 else len(text)
        text = text[:line_start] + text[end:]
        pc = text.rfind(",", 0, line_start)
        if pc != -1:
            text = text[:pc] + text[pc + 1:]
    _write_file(p, text)


def _km_add_entry(target_rel, entry):
    p = _settings_path(target_rel)
    text = _read_file(p)
    nl = _line_ending(text)
    try:
        m = _km_model(text)
        vals = m.value.values if m and m.value and hasattr(m.value, "values") else []
    except Exception:
        vals = []
    body = json.dumps(entry)
    if not vals:
        _write_file(p, "[" + nl + "\t" + body + nl + "]")
        return
    last = vals[-1]
    e = _pos(text, last.end_lineno, last.end_col_offset)
    indent = _km_indent_for(text, m)
    # don't double up a comma if the last entry already has a trailing one
    sep = "" if text[e:e + 1] == "," else ","
    text = text[:e] + sep + nl + indent + body + text[e:]
    _write_file(p, text)


def _km_write_targets():
    plat = _platform_name()
    rels = ["Default.sublime-keymap", "Default (%s).sublime-keymap" % plat]
    out = []
    for r in rels:
        out.append({"rel": r, "label": r, "exists": os.path.exists(_settings_path(r))})
    return out


def _km_resolve(idx):
    cat = _keymap_catalog()
    recs = cat["bindings"]
    if idx < 0 or idx >= len(recs):
        return None
    return recs[idx]


# --- HTTP --------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _q(self):
        from urllib.parse import urlparse, parse_qs
        q = urlparse(self.path).query
        return parse_qs(q)

    def do_GET(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        if path == "/" or path.startswith("/?"):
            self._send(200, "text/html; charset=utf-8", _HTML)
        elif path == "/api/catalog":
            try:
                self._send(200, "application/json", json.dumps(_build_catalog()))
            except Exception as e:
                self._send(200, "application/json", json.dumps({"error": str(e)}))
        elif path == "/api/setting":
            name = self._q().get("name", [None])[0]
            if not name:
                self._send(400, "application/json", '{"error":"name required"}')
                return
            try:
                self._send(200, "application/json", json.dumps(_detail(name)))
            except Exception as e:
                self._send(200, "application/json", json.dumps({"error": str(e)}))
        elif path == "/api/warnings":
            name = self._q().get("name", [None])[0]
            val = self._q().get("value", ["null"])[0]
            try:
                value = json.loads(val)
            except Exception:
                value = None
            try:
                usage = {
                    "keybindings": _keymap_index().get(name, []),
                    "menus": _menu_reads(name) if name else [],
                    "plugins": [],
                }
                self._send(200, "application/json", json.dumps({"warnings": _warnings(name, value, usage)}))
            except Exception as e:
                self._send(200, "application/json", json.dumps({"warnings": [], "error": str(e)}))
        elif path == "/api/keymap/catalog":
            try:
                self._send(200, "application/json", json.dumps(_keymap_catalog()))
            except Exception as e:
                self._send(200, "application/json", json.dumps({"error": str(e)}))
        elif path == "/api/keymap/binding":
            idx = self._q().get("idx", [None])[0]
            try:
                idx = int(idx) if idx is not None else -1
                self._send(200, "application/json", json.dumps(_keymap_binding(idx)))
            except Exception as e:
                self._send(200, "application/json", json.dumps({"error": str(e)}))
        elif path == "/api/commands":
            try:
                self._send(200, "application/json", json.dumps(sorted(_known_commands())))
            except Exception as e:
                self._send(200, "application/json", json.dumps({"error": str(e)}))
        elif path == "/ping":
            self._send(200, "text/plain", "ok")
        else:
            self._send(404, "text/plain", "nf")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw or b"{}")
        except Exception as e:
            self._send(200, "application/json", json.dumps({"ok": False, "error": "bad payload: %s" % e}))
            return
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        if path == "/api/setting":
            try:
                name = payload.get("name")
                value = payload.get("value")
                target = payload.get("target") or _current_source_rel()
                _apply_set(name, value, target)
                det = _detail(name)
                # recompute warnings for the just-written value
                warnings = _warnings(name, value, det["usage"])
                self._send(200, "application/json", json.dumps({"ok": True, "detail": det, "warnings": warnings}))
            except Exception as e:
                self._send(200, "application/json", json.dumps({"ok": False, "error": str(e)}))
        elif path == "/api/delete":
            try:
                name = payload.get("name")
                target = payload.get("target") or _current_source_rel()
                _apply_delete(name, target)
                self._send(200, "application/json", json.dumps({"ok": True, "detail": _detail(name)}))
            except Exception as e:
                self._send(200, "application/json", json.dumps({"ok": False, "error": str(e)}))
        elif path == "/api/shutdown":
            self._send(200, "application/json", '{"ok":true}')
            try:
                srv = _SERVER[0]
                if srv:
                    threading.Thread(target=srv.shutdown, daemon=True).start()
            except Exception:
                pass
        elif path == "/api/keymap/save":
            try:
                idx = int(payload.get("idx", -1))
                entry = payload.get("entry")
                expect = payload.get("expect_cmd")
                rec = _km_resolve(idx)
                if rec is None:
                    raise Exception("no such binding")
                _km_set_entry(rec["source"], rec["local_index"], entry, expect)
                self._send(200, "application/json", json.dumps({"ok": True, "catalog": _keymap_catalog(), "binding": _keymap_binding(idx)}))
            except Exception as e:
                self._send(200, "application/json", json.dumps({"ok": False, "error": str(e)}))
        elif path == "/api/keymap/delete":
            try:
                idx = int(payload.get("idx", -1))
                expect = payload.get("expect_cmd")
                rec = _km_resolve(idx)
                if rec is None:
                    raise Exception("no such binding")
                _km_delete_entry(rec["source"], rec["local_index"], expect)
                self._send(200, "application/json", json.dumps({"ok": True, "catalog": _keymap_catalog()}))
            except Exception as e:
                self._send(200, "application/json", json.dumps({"ok": False, "error": str(e)}))
        elif path == "/api/keymap/add":
            try:
                target = payload.get("target") or "Default.sublime-keymap"
                entry = payload.get("entry")
                if not isinstance(entry, dict):
                    raise Exception("entry required")
                _km_add_entry(target, entry)
                self._send(200, "application/json", json.dumps({"ok": True, "catalog": _keymap_catalog()}))
            except Exception as e:
                self._send(200, "application/json", json.dumps({"ok": False, "error": str(e)}))
        else:
            self._send(404, "application/json", '{"ok":false,"error":"nf"}')


def _serve():
    try:
        httpd = HTTPServer(("127.0.0.1", _PORT), _Handler)
        _SERVER[0] = httpd
        httpd.serve_forever()
    except Exception:
        _SERVER[0] = None


class SettingsEditorOpenCommand(sublime_plugin.WindowCommand):
    def run(self):
        if _SERVER[0] is None:
            t = threading.Thread(target=_serve, daemon=True)
            t.start()
        webbrowser.open("http://127.0.0.1:%d/" % _PORT)


def plugin_unloaded():
    # ST calls this before reloading the plugin / on quit. Shut the HTTP server
    # down so a reload doesn't leave an orphaned thread holding the port (the
    # "zombie server" problem that stranded port 57323 with stale HTML).
    srv = _SERVER[0]
    _SERVER[0] = None
    if srv is not None:
        try:
            threading.Thread(target=srv.shutdown, daemon=True).start()
        except Exception:
            pass


# --- frontend ----------------------------------------------------------------
_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Sublime Settings</title>
<style>
:root{--accent:#0078d4;--user:#fff7cf;--user-bd:#e0c200;--line:#e5e7eb;--muted:#6b7280;--win:#16a34a;--warn:#b45309;--warn-bg:#fff7ed;--danger:#b91c1c}
*{box-sizing:border-box}
body{margin:0;font:13px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif;background:#f7f8fa;color:#111;height:100vh;display:flex;flex-direction:column}
.topbar{display:flex;align-items:center;gap:10px;padding:9px 14px;background:#fff;border-bottom:1px solid var(--line)}
.tabs{display:flex;gap:2px;padding:2px;background:#f1f3f5;border-radius:7px}
.tabs .tab{padding:4px 12px;border:none;background:transparent;border-radius:5px;cursor:pointer;font:inherit;font-size:12.5px;color:#555}
.tabs .tab:hover{background:#e3e8ee}
.tabs .tab.sel{background:#fff;color:var(--accent);font-weight:600;box-shadow:0 1px 2px rgba(0,0,0,.08)}
.topbar h1{font-size:15px;margin:0;font-weight:600}
.topbar input[type=search]{flex:1;max-width:300px;padding:5px 9px;border:1px solid var(--line);border-radius:6px;font:inherit}
.topbar button{padding:5px 11px;border:1px solid var(--line);background:#fff;border-radius:6px;cursor:pointer;font:inherit}
.topbar button:hover{border-color:var(--accent);color:var(--accent)}
.topbar .stop{margin-left:auto;background:#fff;border-color:#e5b4b4;color:var(--danger)}
.topbar .stop:hover{background:var(--danger);color:#fff;border-color:var(--danger)}
.topbar #status{color:var(--muted);font-size:12px;min-width:70px;text-align:right}
.main{flex:1;display:flex;min-height:0}
.tree{width:280px;flex:0 0 280px;overflow:auto;background:#fff;border-right:1px solid var(--line);padding:6px 0}
.tree details>summary{cursor:pointer;padding:4px 10px;list-style:none;font-weight:600;font-size:12.5px;color:#333;user-select:none}
.tree details>summary::-webkit-details-marker{display:none}
.tree details>summary:before{content:"\25B6";display:inline-block;margin-right:6px;font-size:9px;color:var(--muted);transition:transform .1s}
.tree details[open]>summary:before{transform:rotate(90deg)}
.tree details>summary:hover{background:#f0f6ff}
.tree .leaf{padding:3px 10px 3px 26px;cursor:pointer;font:12.5px/1.4 "Cascadia Code",Consolas,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tree .leaf:hover{background:#f0f6ff}
.tree .leaf.sel{background:#e5f1ff;color:var(--accent)}
.tree .leaf.over:before{content:"\25CF";color:var(--user-bd);margin-right:6px;font-size:9px}
.tree .leaf.use:before{content:"\2691";color:#7c3aed;margin-right:5px;font-size:10px;opacity:.7}
.sheet{flex:1;overflow:auto;padding:14px 18px;min-width:0}
.sheet h2{margin:0 0 10px;font-size:15px;font-family:"Cascadia Code",Consolas,monospace}
.sheet .ph{color:var(--muted);padding:40px 20px;text-align:center}
table.props{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);border-radius:6px;overflow:hidden}
table.props td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
table.props td.k{width:170px;color:var(--muted);font-size:12px;white-space:nowrap}
table.props td.v{font:12.5px/1.4 "Cascadia Code",Consolas,monospace;word-break:break-word}
table.props tr.section td{background:#f1f3f5;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#555}
table.props .win{color:var(--win);font-weight:600}
table.props .row-edit td{background:#fafbff}
.ed-cell{font:12.5px/1.4 "Cascadia Code",Consolas,monospace;padding:4px 7px;border:1px solid var(--line);border-radius:5px;background:#fff;width:100%;max-width:380px}
.ed-cell:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 2px rgba(0,120,212,.15)}
.complex{display:inline-flex;gap:6px;align-items:center}
.complex code{font:12px/1.4 "Cascadia Code",Consolas,monospace;color:#444;background:#f1f3f5;padding:2px 6px;border-radius:4px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:inline-block;vertical-align:middle}
.complex button{padding:2px 8px;border:1px solid var(--line);background:#fff;border-radius:5px;cursor:pointer}
.btn{padding:5px 11px;border:1px solid var(--line);background:#fff;border-radius:6px;cursor:pointer;font:inherit}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.writeto{max-width:380px}
.reads{margin:0;padding-left:16px}
.reads li{font:12px/1.5 "Cascadia Code",Consolas,monospace;margin:2px 0}
.reads .file{color:var(--muted)}
.reads .none{color:var(--muted);list-style:none;margin-left:0}
.warns{margin:8px 0 0;padding:8px 10px;background:var(--warn-bg);border:1px solid #f0c9a0;border-radius:6px;font-size:12.5px;color:var(--warn)}
.warns.ok{background:#f0fdf4;border-color:#bbf7d0;color:#15803d}
.warns ul{margin:4px 0 0;padding-left:16px}
.desc-area{margin-top:14px;padding:12px 16px;background:#fff;border:1px solid var(--line);border-radius:6px}
.desc-area .h{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;font-weight:600;margin:0 0 6px}
.desc-area .body{font-size:13px;color:#222;line-height:1.55}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;z-index:20}
.modal-bg.show{display:flex}
.modal{background:#fff;border-radius:8px;padding:14px 16px;width:540px;max-width:92vw;box-shadow:0 10px 40px rgba(0,0,0,.25)}
.modal h3{margin:0 0 8px;font-size:14px}
.modal textarea{width:100%;height:220px;font:12.5px "Cascadia Code",Consolas,monospace;padding:8px;border:1px solid var(--line);border-radius:6px;resize:vertical}
.modal .err{color:var(--danger);font-size:12px;margin:6px 0;min-height:16px}
.modal .row{display:flex;justify-content:flex-end;gap:8px;margin-top:10px}
.modal button{padding:6px 14px;border:1px solid var(--line);background:#fff;border-radius:6px;cursor:pointer}
.modal button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.modal button.danger{background:var(--danger);color:#fff;border-color:var(--danger)}
.confirm-list{margin:6px 0;padding-left:18px;color:var(--warn)}
</style></head>
<body>
<div class="topbar">
 <div class="tabs">
  <button class="tab sel" id="tab-settings" data-mode="settings">Settings</button>
  <button class="tab" id="tab-keybindings" data-mode="keybindings">Keybindings</button>
 </div>
 <h1 id="title">Sublime Settings</h1>
 <input id="search" type="search" placeholder="Filter settings...">
 <span id="status"></span>
 <button class="stop" id="stopbtn" title="Stop the editor server">Stop</button>
</div>
<div class="main">
 <div class="tree" id="tree"></div>
 <div class="sheet" id="sheet"><div class="ph">Select a setting on the left.</div></div>
</div>
<div class="modal-bg" id="modalbg"><div class="modal">
 <h3 id="modtitle">Edit value</h3>
 <textarea id="modta"></textarea>
 <div class="err" id="moderr"></div>
 <div class="row"><button id="modcancel">Cancel</button><button id="modsave" class="primary">Save</button></div>
</div></div>
<div class="modal-bg" id="cfbg"><div class="modal">
 <h3 id="cftitle">Apply this change?</h3>
 <div id="cfbody"></div>
 <div class="row"><button id="cfcancel">Cancel</button><button id="cfok" class="danger">Apply</button></div>
</div></div>
<datalist id="cmdlist"></datalist>
<script>
let CAT=[], S=[], selName=null, D=null, targets=[], curTarget=null;
let MODE='settings';
let KM=[], KMD=null, kmSelIdx=null, kmIsNew=false, kmTargets=[], kmCurTarget=null, CMDLIST=[], kmOnlyConf=false;
let modCb=null;
const $=id=>document.getElementById(id);
const basename=p=>{const i=Math.max(p.lastIndexOf('/'),p.lastIndexOf('\\'));return i<0?p:p.slice(i+1);};
const esc=s=>String(s===null||s===undefined?'—':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const fmt=v=>{if(v===undefined||v===null) return '—'; try{return JSON.stringify(v);}catch(e){return String(v);}};
function fmtMultiline(v){if(v===undefined||v===null) return '—'; try{return JSON.stringify(v,null,2);}catch(e){return String(v);}}

function renderTree(){
  if(MODE==='keybindings'){renderKmTree();return;}
  const q=$('search').value.toLowerCase().trim();
  const byCat={};
  S.forEach(s=>{if(!q||s.name.toLowerCase().indexOf(q)>=0){(byCat[s.category]=byCat[s.category]||[]).push(s);}});
  let html='';
  CAT.forEach(c=>{
    const list=byCat[c]||[];
    if(!list.length && q) return;
    html+='<details'+(q?' open':'')+'><summary>'+esc(c)+' ('+list.length+')</summary>';
    list.forEach(s=>{
      const cls=['leaf']+(s.name===selName?' sel':'');
      const marks=(s.overridden?' over':'')+(s.has_usage?' use':'');
      html+='<div class="'+cls.trim()+marks+'" data-name="'+esc(s.name)+'" title="'+esc(s.name)+(s.overridden?' · overridden':'')+(s.has_usage?' · read by keybinding':'')+'">'+esc(s.name)+'</div>';
    });
    html+='</details>';
  });
  $('tree').innerHTML=html;
  $('tree').querySelectorAll('.leaf').forEach(el=>el.addEventListener('click',()=>select(el.dataset.name)));
}

function select(name){
  selName=name;
  $('tree').querySelectorAll('.leaf').forEach(el=>el.classList.toggle('sel',el.dataset.name===name));
  $('status').textContent='loading...';
  fetch('/api/setting?name='+encodeURIComponent(name)).then(r=>r.json()).then(d=>{
    if(d.error){$('sheet').innerHTML='<div class="ph">Error: '+esc(d.error)+'</div>';return;}
    D=d; targets=d.write_targets||[]; curTarget=d.current_source||'Preferences.sublime-settings';
    renderSheet();
    $('status').textContent='';
  }).catch(e=>{$('status').textContent='error';console.error(e);});
}

function valEditorHTML(){
  const v=D.effective, t=D.type, en=D.enum;
  const ed='id="ved" data-name="'+esc(D.name)+'"';
  switch(t){
    case 'bool': return '<input type="checkbox" '+ed+(v?'checked':'')+'>';
    case 'enum': return '<select '+ed+'>'+(en||[]).map(o=>'<option value="'+esc(o)+'"'+(o===v?' selected':'')+'>'+esc(o)+'</option>').join('')+'</select>';
    case 'int': return '<input type="number" step="1" class="ed-cell" '+ed+' value="'+esc(v===null?'':v)+'">';
    case 'float': return '<input type="number" step="any" class="ed-cell" '+ed+' value="'+esc(v===null?'':v)+'">';
    case 'string': return '<input type="text" class="ed-cell" '+ed+' value="'+esc(v===null?'':v)+'">';
    case 'array':
    case 'object': return '<span class="complex"><button id="vedbtn">'+(t==='array'?'[…]':'{…}')+'</button><code>'+esc(fmt(v))+'</code></span>';
    default: return '<input type="text" class="ed-cell" '+ed+' value="'+esc(v===null?'':v)+'">';
  }
}
function valueFromEditor(){
  const el=$('ved');
  if(!el){
    // complex: read from modal-saved value stored on D
    return D._pending!==undefined?D._pending:D.effective;
  }
  switch(D.type){
    case 'bool': return el.checked;
    case 'int': {const n=parseInt(el.value,10); return el.value===''?null:(isNaN(n)?el.value:n);}
    case 'float': {const n=parseFloat(el.value); return el.value===''?null:(isNaN(n)?el.value:n);}
    case 'enum': return el.value;
    default: return el.value;
  }
}

function chainHTML(){
  const ch=D.override_chain||[];
  if(!ch.length) return '<span class="none">no global layers define it</span>';
  return ch.map(e=>'<div>'+(e.wins?'<span class="win">▸ '+esc(e.layer)+'</span>':'&nbsp;&nbsp; '+esc(e.layer))+
    ' = '+esc(fmt(e.value))+' <span class="file">('+esc(e.source)+')</span></div>').join('');
}
function readsHTML(label, items, kind){
  if(!items||!items.length) return '<li class="none">none found</li>';
  return items.slice(0,20).map(h=>{
    if(kind==='key'){
      const keys=Array.isArray(h.keys)?h.keys.join('+'):(h.keys||'?');
      return '<li><b>'+esc(h.command||'?')+'</b> <span class="file">'+esc(keys)+' — '+esc(h.file)+(h.operator?(' ['+esc(h.operator)+' '+esc(fmt(h.operand))+']'):'')+'</span></li>';
    }
    if(kind==='menu'){
      return '<li><b>'+esc(h.command||'?')+'</b> <span class="file">'+esc(h.caption||'')+' — '+esc(h.file)+'</span></li>';
    }
    return '<li><span class="file">'+esc(h.file)+':'+h.line+'</span> '+esc(h.snippet)+'</li>';
  }).join('')+(items.length>20?'<li class="none">... '+(items.length-20)+' more</li>':'');
}

function renderSheet(){
  const t=$('sheet');
  const wopts=targets.map(tt=>'<option value="'+esc(tt.rel)+'"'+(tt.rel===curTarget?' selected':'')+'>'+esc(tt.label)+(tt.exists?'':' (new)')+'</option>').join('');
  const eff=D.effective;
  t.innerHTML='<h2>'+esc(D.name)+'</h2>'+
   '<table class="props">'+
    row('Category',esc(D.category))+
    row('Type',esc(D.type)+(D.enum?(' <span class="file">(allowed: '+esc(D.enum.join(', '))+')</span>'):''))+
    row('Default',esc(fmt(D.default)))+
    row('Effective',esc(fmt(eff))+' <span class="file">(from active view)</span>')+
    row('Owner',esc(D.owner))+
    sec('Override chain (winner marked)')+
    '<tr><td class="k"></td><td class="v">'+chainHTML()+'</td></tr>'+
    sec('Value')+
    '<tr class="row-edit"><td class="k">Value</td><td class="v">'+valEditorHTML()+'</td></tr>'+
    '<tr class="row-edit"><td class="k">Write to</td><td class="v"><select class="ed-cell writeto" id="wtarget">'+wopts+'</select></td></tr>'+
    '<tr class="row-edit"><td class="k"></td><td class="v"><button class="btn" id="restore">Restore Default</button></td></tr>'+
    sec('Consequences if this value is applied')+
    '<tr><td class="k"></td><td class="v"><div class="warns ok" id="warns">checking...</div></td></tr>'+
    sec('Reads — keybindings (context setting.'+esc(D.name)+')')+
    '<tr><td class="k"></td><td class="v"><ul class="reads">'+readsHTML('kb',D.usage.keybindings,'key')+'</ul></td></tr>'+
    sec('Reads — menus')+
    '<tr><td class="k"></td><td class="v"><ul class="reads">'+readsHTML('menu',D.usage.menus,'menu')+'</ul></td></tr>'+
    sec('Reads — plugin code (loose Packages only)')+
    '<tr><td class="k"></td><td class="v"><ul class="reads">'+readsHTML('py',D.usage.plugins,'py')+'</ul></td></tr>'+
   '</table>'+
   '<div class="desc-area"><p class="h">Description / help</p><div class="body">'+esc(D.desc||'(no description available)')+'</div></div>';
  bindSheet();
  refreshWarns(valueFromEditor());
}

function row(k,v){return '<tr><td class="k">'+esc(k)+'</td><td class="v">'+v+'</td></tr>';}
function sec(label){return '<tr class="section"><td colspan="2">'+esc(label)+'</td></tr>';}

function bindSheet(){
  const el=$('ved');
  if(el){
    const ev=(el.type==='checkbox'||el.tagName==='SELECT')?'change':'change';
    el.addEventListener(ev,()=>{commit();});
    if(el.type==='text'||el.type==='number'){el.addEventListener('keydown',e=>{if(e.key==='Enter')commit();});}
  }
  const vb=$('vedbtn');
  if(vb) vb.addEventListener('click',openModal);
  const wt=$('wtarget');
  if(wt) wt.addEventListener('change',()=>{curTarget=wt.value;});
  const rb=$('restore');
  if(rb) rb.addEventListener('click',()=>{
    if(!confirm('Delete '+D.name+' from '+curTarget+' so the default takes effect?')) return;
    fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:D.name,target:curTarget})})
     .then(r=>r.json()).then(r=>{if(r.ok){reloadCatalogThenSelect();}else{alert(r.error||'error');}});
  });
}

function commit(){
  const value=valueFromEditor();
  refreshWarns(value);
  // gather warnings, confirm if any, then write
  fetch('/api/warnings?name='+encodeURIComponent(D.name)+'&value='+encodeURIComponent(JSON.stringify(value)))
    .then(r=>r.json()).then(w=>{
      const warns=w.warnings||[];
      if(warns.length){
        $('cftitle').textContent='Apply change to '+D.name+'?';
        $('cfbody').innerHTML='<div>Writing <code>'+esc(fmt(value))+'</code> to <code>'+esc(curTarget)+'</code>.</div>'+
          '<div style="margin:8px 0 4px;font-weight:600;color:var(--warn)">Consequences to consider:</div>'+
          '<ul class="confirm-list">'+warns.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>';
        $('cfbg').classList.add('show');
        $('cfok').onclick=()=>{$('cfbg').classList.remove('show');doWrite(value);};
      } else {
        doWrite(value);
      }
    });
}

function doWrite(value){
  $('status').textContent='saving...';
  fetch('/api/setting',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:D.name,value:value,target:curTarget})})
   .then(r=>r.json()).then(r=>{
     if(r.ok){D=r.detail;D._pending=undefined;renderSheet();reloadCatalog();$('status').textContent='saved';setTimeout(()=>$('status').textContent='',1200);}
     else {$('status').textContent='error';alert(r.error||'error');}
   }).catch(e=>{$('status').textContent='error';console.error(e);});
}

function refreshWarns(value){
  const w=$('warns'); if(!w) return;
  fetch('/api/warnings?name='+encodeURIComponent(D.name)+'&value='+encodeURIComponent(JSON.stringify(value)))
    .then(r=>r.json()).then(r=>{
      const warns=r.warnings||[];
      if(!warns.length){w.className='warns ok';w.innerHTML='✓ no warnings for this value';}
      else{w.className='warns';w.innerHTML='<ul>'+warns.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>';}
    });
}

function reloadCatalog(){fetch('/api/catalog').then(r=>r.json()).then(c=>{S=c.settings||[];CAT=c.categories||[];renderTree();});}
function reloadCatalogThenSelect(){fetch('/api/catalog').then(r=>r.json()).then(c=>{S=c.settings||[];CAT=c.categories||[];renderTree();select(D.name);});}

let modName=null;
function openModal(){
  modCb=null; modName=D.name;
  $('modtitle').textContent='Edit '+D.name+' ('+D.type+')';
  $('modta').value=fmtMultiline(D._pending!==undefined?D._pending:D.effective);
  $('moderr').textContent=''; $('modalbg').classList.add('show');
}
$('modsave').addEventListener('click',()=>{
  try{const v=JSON.parse($('modta').value); $('modalbg').classList.remove('show');
    if(modCb){modCb(v);modCb=null;}
    else {D._pending=v;
      // re-render the value row with the new complex value, then commit
      const code=$('sheet').querySelector('.complex code'); if(code) code.textContent=fmt(v);
      commit();}
  }catch(e){$('moderr').textContent='Invalid JSON: '+e.message;}
});
$('modcancel').addEventListener('click',()=>{modCb=null;$('modalbg').classList.remove('show');});
$('cfcancel').addEventListener('click',()=>$('cfbg').classList.remove('show'));

$('search').addEventListener('input',renderTree);
$('stopbtn').addEventListener('click',()=>{
  if(!confirm('Stop the settings editor server?')) return;
  fetch('/api/shutdown',{method:'POST'}).then(()=>$('sheet').innerHTML='<div class="ph">Server stopped. Close this tab.</div>');
});

// --- keybindings tab --------------------------------------------------------
function loadCommands(){
  fetch('/api/commands').then(r=>r.json()).then(l=>{CMDLIST=l||[];const dl=$('cmdlist');if(dl)dl.innerHTML=CMDLIST.map(n=>'<option value="'+esc(n)+'">').join('');});
}
function loadKm(){
  loadCommands();
  fetch('/api/keymap/catalog').then(r=>r.json()).then(c=>{
    KM=c.bindings||[]; kmTargets=c.write_targets||[]; if(!kmCurTarget)kmCurTarget='Default.sublime-keymap';
    renderKmTree();
    if(KM.length) selectKm(KM[0].idx); else $('sheet').innerHTML='<div class="ph">No keybindings found.</div>';
  });
}
function kmGrouped(){
  const q=$('search').value.toLowerCase().trim();
  const onlyConf=kmOnlyConf;
  const byPkg={};
  KM.forEach(b=>{
    if(onlyConf&&!b.conflict_count) return;
    if(q&&(b.chord||'').toLowerCase().indexOf(q)<0&&((b.command||'')).toLowerCase().indexOf(q)<0) return;
    (byPkg[b.pkg]=byPkg[b.pkg]||[]).push(b);
  });
  return byPkg;
}
function renderKmTree(){
  const byPkg=kmGrouped();
  const pkgs=Object.keys(byPkg).sort((a,b)=>(a!=='User')-(b!=='User')||a.localeCompare(b));
  const header='<div style="padding:6px 10px;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center">'+
    '<label style="font-size:11px;color:var(--muted);display:flex;align-items:center;gap:3px"><input type="checkbox" id="kmonlyconf"> conflicts</label>'+
    '<button class="btn" id="kmadd" style="margin-left:auto;padding:3px 9px;font-size:11px">+ Add binding</button></div>';
  let html='';
  pkgs.forEach(p=>{
    const list=byPkg[p];
    html+='<details open><summary>'+esc(p)+' ('+list.length+')</summary>';
    list.forEach(b=>{
      const cls=['leaf']; if(b.idx===kmSelIdx) cls.push('sel');
      html+='<div class="'+cls.join(' ').trim()+'" data-idx="'+b.idx+'" title="'+esc(b.chord)+' → '+esc(b.command||'?')+(b.conflict_count?' · '+b.conflict_count+' conflict(s)':'')+(b.dead?' · dead':'')+'">'+
        esc(b.chord||'(no keys)')+' → '+esc(b.command||'?')+
        (b.conflict_count?' <span style="color:var(--warn)">⚠</span>':'')+
        (b.dead?' <span style="color:var(--danger)">✗</span>':'')+'</div>';
    });
    html+='</details>';
  });
  if(!pkgs.length) html+='<div class="ph" style="padding:14px">No bindings match.</div>';
  $('tree').innerHTML=header+html;
  const cb=$('kmonlyconf'); if(cb){cb.checked=kmOnlyConf;cb.addEventListener('change',()=>{kmOnlyConf=cb.checked;renderKmTree();});}
  const ab=$('kmadd'); if(ab)ab.addEventListener('click',newKm);
  $('tree').querySelectorAll('.leaf').forEach(el=>el.addEventListener('click',()=>selectKm(parseInt(el.dataset.idx,10))));
}
function selectKm(idx){
  kmSelIdx=idx; kmIsNew=false;
  $('tree').querySelectorAll('.leaf').forEach(el=>el.classList.toggle('sel',parseInt(el.dataset.idx,10)===idx));
  $('status').textContent='loading...';
  fetch('/api/keymap/binding?idx='+idx).then(r=>r.json()).then(d=>{
    if(d.error){$('sheet').innerHTML='<div class="ph">Error: '+esc(d.error)+'</div>';return;}
    KMD=d; kmTargets=d.write_targets||[];
    kmCurTarget = d.is_user ? basename(d.source) : 'Default.sublime-keymap';
    renderKmSheet(); $('status').textContent='';
  });
}
function cmdCell(d,ro){
  const v=d.command==null?'':d.command;
  return '<input type="text" class="ed-cell" id="kmcmd" list="cmdlist" value="'+esc(v)+'" '+(ro?'disabled':'')+'>'+
    (d.dead?' <span style="color:var(--danger)">✗ dead</span>':'');
}
function keysCell(d,ro){return '<input type="text" class="ed-cell" id="kmkeys" value="'+esc(d.chord||'')+'" '+(ro?'disabled':'')+'>';}
function jsonCell(field,d,ro,label){
  const v=(d['_p_'+field]!==undefined?d['_p_'+field]:d[field]);
  return '<span class="complex"><button id="km'+field+'btn" '+(ro?'disabled':'')+'>'+label+'</button><code>'+esc(fmt(v))+'</code></span>';
}
function kmConflictsHTML(d){
  const cs=d.conflicts||[];
  if(!cs.length) return '<span class="none">no other bindings share this chord</span>';
  return '<ul class="reads">'+cs.map(c=>'<li><b>'+esc(c.command||'?')+'</b> <span class="file">'+esc((c.keys||[]).join(' '))+' — '+esc(c.source)+(c.is_user?'':' (pkg)')+'</span></li>').join('')+'</ul>';
}
function renderKmSheet(){
  const d=KMD, ro=!d.writable;
  const wopts=kmTargets.map(t=>'<option value="'+esc(t.rel)+'"'+(t.rel===kmCurTarget?' selected':'')+'>'+esc(t.label)+(t.exists?'':' (new)')+'</option>').join('');
  $('sheet').innerHTML='<h2>'+(kmIsNew?'(new binding) ':d.is_user?'':('<span style="color:var(--danger)">(read-only) </span>'))+esc(d.chord||'(no keys)')+'</h2>'+
   '<table class="props">'+
    row('Source',esc(d.source)+(d.writable?' <span class="file">(writable)</span>':' <span class="file">(read-only — package file)</span>'))+
    row('Command',cmdCell(d,ro))+
    row('Keys',keysCell(d,ro))+
    row('Args',jsonCell('args',d,ro,'{…}'))+
    row('Context',jsonCell('context',d,ro,'[…]'))+
    sec('Write to')+
    '<tr class="row-edit"><td class="k"></td><td class="v"><select class="ed-cell writeto" id="kmwtarget" '+(ro?'disabled':'')+'>'+wopts+'</select></td></tr>'+
    '<tr class="row-edit"><td class="k"></td><td class="v">'+(ro?'<span class="file">Package bindings are read-only. Add an override in User to change behaviour.</span>':'<button class="btn" id="kmsave">Save</button>'+(kmIsNew?'':' <button class="btn" id="kmdelete" style="border-color:#e5b4b4;color:var(--danger)">Delete</button>'))+'</td></tr>'+
    sec('Conflicts (same chord)')+
    '<tr><td class="k"></td><td class="v">'+kmConflictsHTML(d)+'</td></tr>'+
   '</table>'+
   '<div class="desc-area"><p class="h">Command info</p><div class="body">'+(d.dead?'<span style="color:var(--danger)">Not registered at runtime (dead reference).</span>':(d.command_source?'defined in <code>'+esc(d.command_source)+'</code>':'registered at runtime'))+'</div></div>';
  bindKmSheet();
}
function openKmField(field){
  modCb=(v)=>{KMD['_p_'+field]=v;const code=document.querySelector('#km'+field+'btn').parentNode.querySelector('code');if(code)code.textContent=fmt(v);};
  $('modtitle').textContent='Edit '+field+' (JSON)';
  $('modta').value=fmtMultiline(KMD['_p_'+field]!==undefined?KMD['_p_'+field]:KMD[field]);
  $('moderr').textContent=''; $('modalbg').classList.add('show');
}
function bindKmSheet(){
  const wt=$('kmwtarget'); if(wt)wt.addEventListener('change',()=>kmCurTarget=wt.value);
  const ab=$('kmargsbtn'); if(ab)ab.addEventListener('click',()=>openKmField('args'));
  const cb=$('kmctxbtn'); if(cb)cb.addEventListener('click',()=>openKmField('context'));
  const sv=$('kmsave'); if(sv)sv.addEventListener('click',kmSave);
  const dl=$('kmdelete'); if(dl)dl.addEventListener('click',kmDelete);
}
function kmEntryFromSheet(){
  const keysVal=$('kmkeys')?$('kmkeys').value.trim():'';
  const keys=keysVal?keysVal.split(/\s+/):[];
  const cmd=$('kmcmd')?$('kmcmd').value.trim():'';
  const entry={"keys":keys};
  if(cmd) entry.command=cmd;
  const a=KMD['_p_args']!==undefined?KMD['_p_args']:KMD.args;
  if(a!==undefined&&a!==null) entry.args=a;
  const c=KMD['_p_context']!==undefined?KMD['_p_context']:KMD.context;
  if(c!==undefined&&c!==null) entry.context=c;
  return entry;
}
function kmSave(){
  const entry=kmEntryFromSheet();
  if(!entry.keys.length){alert('Keys are required.');return;}
  if(!entry.command){alert('Command is required.');return;}
  if(kmIsNew){
    fetch('/api/keymap/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:kmCurTarget,entry:entry})})
     .then(r=>r.json()).then(r=>{if(r.ok){KM=r.catalog.bindings||[];renderKmTree();kmIsNew=false;$('status').textContent='added';setTimeout(()=>$('status').textContent='',1200);}else{alert(r.error||'error');}});
    return;
  }
  if(!KMD.writable){alert('This binding is in a package file (read-only).');return;}
  $('status').textContent='saving...';
  fetch('/api/keymap/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({idx:KMD.idx,entry:entry,expect_cmd:KMD.command})})
   .then(r=>r.json()).then(r=>{if(r.ok){KM=r.catalog.bindings||[];renderKmTree();if(r.binding){KMD=r.binding;renderKmSheet();}$('status').textContent='saved';setTimeout(()=>$('status').textContent='',1200);}else{$('status').textContent='error';alert(r.error||'error');}});
}
function kmDelete(){
  if(!KMD.writable){alert('Read-only.');return;}
  if(!confirm('Delete this binding from '+basename(KMD.source)+'?'))return;
  fetch('/api/keymap/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({idx:KMD.idx,expect_cmd:KMD.command})})
   .then(r=>r.json()).then(r=>{if(r.ok){KM=r.catalog.bindings||[];kmSelIdx=null;renderKmTree();$('sheet').innerHTML='<div class="ph">Binding deleted. Select another on the left.</div>';}else{alert(r.error||'error');}});
}
function newKm(){
  kmIsNew=true; kmSelIdx=null;
  $('tree').querySelectorAll('.leaf').forEach(el=>el.classList.remove('sel'));
  KMD={keys:[],chord:'',command:'',args:undefined,context:undefined,source:'(new in User/'+kmCurTarget+')',is_user:true,writable:true,dead:true,command_source:null,conflicts:[]};
  renderKmSheet();
  $('sheet').querySelector('h2').textContent='(new binding)';
}
function switchMode(m){
  MODE=m;
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('sel',t.dataset.mode===m));
  $('title').textContent = m==='keybindings'?'Keybindings':'Sublime Settings';
  $('search').placeholder = m==='keybindings'?'Filter by chord or command…':'Filter settings...';
  $('search').value=''; selName=null; kmSelIdx=null; D=null; KMD=null;
  $('sheet').innerHTML='<div class="ph">Select an item on the left.</div>';
  if(m==='keybindings') loadKm(); else load();
}
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>switchMode(t.dataset.mode)));

function load(){
  fetch('/api/catalog').then(r=>r.json()).then(c=>{
    S=c.settings||[]; CAT=c.categories||[]; targets=c.write_targets||[]; curTarget=c.current_source||'Preferences.sublime-settings';
    renderTree();
    if(S.length) select(S[0].name);
  });
}
load();
</script>
</body></html>
"""