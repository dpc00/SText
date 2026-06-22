"""st_config.py — Universal ST Configuration browser.

Tabs: Keybindings | Commands | Menus
HTTP server in-process. Opens in the default browser.
"""

import json
import os
import re
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import sublime
import sublime_plugin

_FIXED_PORT = 57322


# ── platform ──────────────────────────────────────────────────────────────────


def _plat_label():
    return {"windows": "Windows", "osx": "OSX", "linux": "Linux"}.get(
        sublime.platform(), "Windows"
    )


def _keymap_fnames():
    return {"Default.sublime-keymap", f"Default ({_plat_label()}).sublime-keymap"}


def _user_keymap_path():
    return os.path.join(
        sublime.packages_path(), "User", f"Default ({_plat_label()}).sublime-keymap"
    )


def _user_commands_path():
    return os.path.join(sublime.packages_path(), "User", "Default.sublime-commands")


# ── keybindings ───────────────────────────────────────────────────────────────


def _is_kb_sep(keys):
    return isinstance(keys, list) and len(keys) == 1 and keys[0] in ("--", "-")


def _load_all_bindings():
    fnames = _keymap_fnames()
    out, seen = [], set()
    for path in sublime.find_resources("*.sublime-keymap"):
        if path.split("/")[-1] not in fnames or path in seen:
            continue
        seen.add(path)
        m = re.match(r"Packages/([^/]+)/", path)
        pkg = m.group(1) if m else "?"
        try:
            entries = sublime.decode_value(sublime.load_resource(path))
            if not isinstance(entries, list):
                continue
        except Exception:
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            keys = e.get("keys")
            if not keys:
                continue
            cmd = e.get("command", "")
            is_sep = _is_kb_sep(keys)
            out.append(
                {
                    "keys": keys,
                    "key_str": ", ".join(keys) if isinstance(keys, list) else str(keys),
                    "command": cmd,
                    "args": e.get("args"),
                    "context": e.get("context"),
                    "source": pkg,
                    "is_user": pkg == "User",
                    "is_sep": is_sep,
                }
            )
    return out


def _read_user_keymap():
    try:
        raw = Path(_user_keymap_path()).read_text(encoding="utf-8")
        r = sublime.decode_value(raw)
        return r if isinstance(r, list) else []
    except Exception:
        return []


def _write_user_keymap(entries):
    path = Path(_user_keymap_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["["]
    for i, e in enumerate(entries):
        obj = {"keys": e["keys"]}
        if e.get("command"):
            obj["command"] = e["command"]
        if e.get("args") is not None:
            obj["args"] = e["args"]
        if e.get("context") is not None:
            obj["context"] = e["context"]
        lines.append(
            f"\t{json.dumps(obj, ensure_ascii=False)}{',' if i < len(entries)-1 else ''}"
        )
    lines.append("]")
    path.write_text("\n".join(lines), encoding="utf-8")


# ── commands ──────────────────────────────────────────────────────────────────


def _load_all_commands():
    out, seen = [], set()
    for path in sublime.find_resources("*.sublime-commands"):
        m = re.match(r"Packages/([^/]+)/", path)
        if not m:
            continue
        pkg = m.group(1)
        try:
            entries = sublime.decode_value(sublime.load_resource(path))
            if not isinstance(entries, list):
                continue
        except Exception:
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            caption = e.get("caption", "")
            cmd = e.get("command", "")
            if not caption and not cmd:
                continue
            uid = f"{path}::{caption}::{cmd}"
            if uid in seen:
                continue
            seen.add(uid)
            out.append(
                {
                    "caption": caption,
                    "command": cmd,
                    "args": e.get("args"),
                    "source": pkg,
                    "is_user": pkg == "User",
                    "is_sep": caption == "-",
                }
            )
    return out


def _read_user_commands():
    try:
        raw = Path(_user_commands_path()).read_text(encoding="utf-8")
        r = sublime.decode_value(raw)
        return r if isinstance(r, list) else []
    except Exception:
        return []


def _write_user_commands(entries):
    path = Path(_user_commands_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["["]
    for i, e in enumerate(entries):
        obj = {}
        if e.get("caption") is not None:
            obj["caption"] = e["caption"]
        if e.get("command"):
            obj["command"] = e["command"]
        if e.get("args") is not None:
            obj["args"] = e["args"]
        lines.append(
            f"\t{json.dumps(obj, ensure_ascii=False)}{',' if i < len(entries)-1 else ''}"
        )
    lines.append("]")
    path.write_text("\n".join(lines), encoding="utf-8")


# ── menus ─────────────────────────────────────────────────────────────────────

_MENU_FILES = [
    "Main.sublime-menu",
    "Context.sublime-menu",
    "Tab Context.sublime-menu",
    "Side Bar.sublime-menu",
    "Find in Files.sublime-menu",
    "Widget Context.sublime-menu",
]


def _flatten_menu(items, breadcrumb, pkg, out):
    for item in items:
        if not isinstance(item, dict):
            continue
        caption = item.get("caption", "")
        cmd = item.get("command", "")
        children = item.get("children")
        if caption == "-":
            continue
        current = breadcrumb + ([caption] if caption else [])
        if caption or cmd:
            out.append(
                {
                    "path": " › ".join(breadcrumb),
                    "caption": caption,
                    "command": cmd,
                    "args": item.get("args"),
                    "source": pkg,
                }
            )
        if children:
            _flatten_menu(children, current, pkg, out)


def _load_all_menus():
    result = {}
    for fname in _MENU_FILES:
        items = []
        for path in sublime.find_resources(fname):
            m = re.match(r"Packages/([^/]+)/", path)
            pkg = m.group(1) if m else "?"
            try:
                tree = sublime.decode_value(sublime.load_resource(path))
                if not isinstance(tree, list):
                    continue
            except Exception:
                continue
            _flatten_menu(tree, [], pkg, items)
        if items:
            result[fname.replace(".sublime-menu", "")] = items
    return result


def _read_user_menus():
    user_dir = Path(sublime.packages_path()) / "User"
    result = {}
    for fname in _MENU_FILES:
        path = user_dir / fname
        if not path.exists():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            entries = sublime.decode_value(raw)
            if isinstance(entries, list):
                result[fname.replace(".sublime-menu", "")] = entries
        except Exception:
            pass
    return result


def _write_user_menu(name, entries):
    path = Path(sublime.packages_path()) / "User" / f"{name}.sublime-menu"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["["]
    for i, e in enumerate(entries):
        obj = {}
        if e.get("caption") is not None:
            obj["caption"] = e["caption"]
        if e.get("command"):
            obj["command"] = e["command"]
        if e.get("args") is not None:
            obj["args"] = e["args"]
        if e.get("children"):
            obj["children"] = e["children"]
        lines.append(
            f"\t{json.dumps(obj, ensure_ascii=False)}{',' if i < len(entries)-1 else ''}"
        )
    lines.append("]")
    path.write_text("\n".join(lines), encoding="utf-8")


# ── combined data ─────────────────────────────────────────────────────────────


def _build_data():
    bindings = _load_all_bindings()
    key_count = defaultdict(int)
    for b in bindings:
        if not b["is_sep"]:
            key_count[b["key_str"]] += 1
    for b in bindings:
        b["conflict"] = (not b["is_sep"]) and key_count[b["key_str"]] > 1
    return {
        "kb": {"bindings": bindings, "user_entries": _read_user_keymap()},
        "cmds": {
            "bindings": _load_all_commands(),
            "user_entries": _read_user_commands(),
        },
        "menus": _load_all_menus(),
        "user_menus": _read_user_menus(),
        "platform": _plat_label(),
    }


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ST Config</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --accent:#0078d4;--accent-dk:#106ebe;--accent-lt:#e5f3ff;
  --bg:#f3f3f3;--panel:#fff;--border:#d1d1d1;--border-lt:#ebebeb;
  --text:#1b1b1b;--muted:#6e6e6e;
  --user-bg:#fffbe6;--user-bdr:#f0a500;
  --warn:#c42b1c;
}
html,body{height:100%;overflow:hidden}
body{font-family:"Segoe UI",system-ui,sans-serif;font-size:13px;
     background:var(--bg);color:var(--text);display:flex;flex-direction:column}

header{height:52px;background:var(--accent);color:#fff;
       display:flex;align-items:center;padding:0 16px;gap:10px;
       flex-shrink:0;box-shadow:0 2px 6px rgba(0,0,0,.25)}
header h1{font-size:15px;font-weight:600;white-space:nowrap;letter-spacing:.2px}
.search-wrap{position:relative;flex:1;max-width:360px}
.search-wrap svg{position:absolute;left:9px;top:50%;transform:translateY(-50%);
                 opacity:.75;pointer-events:none}
#search{width:100%;padding:6px 10px 6px 32px;border:none;border-radius:4px;
        background:rgba(255,255,255,.18);color:#fff;font-size:13px;font-family:inherit;outline:none}
#search::placeholder{color:rgba(255,255,255,.6)}
#search:focus{background:rgba(255,255,255,.28);box-shadow:0 0 0 2px rgba(255,255,255,.4)}
.spacer{flex:1}
.hdr-btn{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.35);
         padding:6px 14px;border-radius:4px;cursor:pointer;font-size:13px;
         font-family:inherit;white-space:nowrap;transition:background .15s}
.hdr-btn:hover{background:rgba(255,255,255,.28)}
.hdr-btn.primary{background:rgba(255,255,255,.3);border-color:rgba(255,255,255,.6);font-weight:600}

.tab-bar{height:36px;background:var(--panel);border-bottom:2px solid var(--border);
         display:flex;align-items:stretch;padding:0 8px;flex-shrink:0}
.tab{padding:0 18px;cursor:pointer;font-size:13px;color:var(--muted);
     display:flex;align-items:center;gap:5px;
     border-bottom:2px solid transparent;margin-bottom:-2px;
     user-select:none;transition:color .1s}
.tab:hover{color:var(--text)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}

.body-area{flex:1;display:flex;flex-direction:column;overflow:hidden}
.tab-section{flex:1;display:flex;flex-direction:column;overflow:hidden}
.tab-section.hidden{display:none}

.filter-bar{height:38px;display:flex;align-items:center;gap:8px;padding:0 16px;
            background:var(--panel);border-bottom:1px solid var(--border);flex-shrink:0;
            overflow-x:auto}
.chip{padding:3px 12px;border-radius:20px;font-size:12px;cursor:pointer;
      border:1px solid var(--border);background:var(--bg);color:var(--muted);
      transition:all .1s;user-select:none;display:flex;align-items:center;gap:5px;
      white-space:nowrap;flex-shrink:0}
.chip.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.chip .cnt{font-size:10px;background:var(--accent);color:#fff;
           border-radius:10px;padding:0 5px;min-width:16px;text-align:center}
.chip.active .cnt{background:rgba(255,255,255,.3)}
.chip.warn .cnt{background:var(--warn)}
.chip.warn.active{background:var(--warn);border-color:var(--warn)}
.chip.warn.active .cnt{background:rgba(255,255,255,.3)}

main{flex:1;overflow-y:auto}
table{width:100%;border-collapse:collapse;background:var(--panel)}
thead th{background:#efefef;padding:7px 12px;font-size:11px;font-weight:700;
         text-transform:uppercase;letter-spacing:.5px;color:var(--muted);
         text-align:left;white-space:nowrap;position:sticky;top:0;z-index:5;
         border-bottom:2px solid var(--border)}
tbody tr{border-bottom:1px solid var(--border-lt);transition:background .08s}
tbody tr:hover{background:#f8f8f8}
tbody tr.user-row{background:var(--user-bg)}
tbody tr.user-row:hover{background:#fff5cc}
tbody tr.user-row td:first-child{border-left:3px solid var(--user-bdr)}
tbody tr.conflict:not(.user-row) td:first-child{border-left:3px solid var(--warn)}
tbody tr.hidden{display:none}
td{padding:6px 12px;vertical-align:middle}

.sep-row td{padding:0!important;height:20px}
.sep-row:hover{background:var(--user-bg)!important}
.sep-row td:first-child{border-left:3px solid var(--user-bdr)}
.sep-inner{display:flex;align-items:center;height:20px;padding:0 12px}
.sep-line{flex:1;height:1px;background:var(--border)}
.sep-label{font-size:10px;color:var(--muted);padding:0 8px;white-space:nowrap}

.key-seq{display:inline-flex;align-items:center;gap:2px;flex-wrap:wrap}
.chord-arr{font-size:10px;color:var(--muted);margin:0 3px}
kbd{background:#fff;border:1px solid #c5c5c5;border-bottom:2px solid #a8a8a8;
    border-radius:3px;padding:1px 5px;
    font-family:"Cascadia Code","Consolas","Courier New",monospace;
    font-size:11px;color:#222;display:inline-block;line-height:1.5}
.plus{font-size:10px;color:var(--muted);margin:0 1px}
.mono{font-family:"Cascadia Code","Consolas","Courier New",monospace;font-size:12px}
.muted-sm{font-size:11px;color:var(--muted)}
.trunc{max-width:160px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ctx-btn{background:var(--accent-lt);color:var(--accent);border:1px solid var(--accent);
         border-radius:3px;padding:1px 6px;font-size:10px;cursor:pointer;
         font-family:inherit;transition:all .1s}
.ctx-btn:hover{background:var(--accent);color:#fff}
.src{font-size:11px;color:var(--muted)}
.src.is-user{color:var(--user-bdr);font-weight:600}

.act-cell{text-align:right;padding-right:8px!important;white-space:nowrap}
.act-btn{background:none;border:none;cursor:pointer;padding:2px 5px;border-radius:3px;
         font-size:13px;color:var(--muted);transition:all .1s;line-height:1}
.act-btn:hover{background:#e8e8e8;color:var(--text)}
.act-btn.del:hover{background:#fde8e8;color:var(--warn)}
.act-btn.del-confirm{background:#fde8e8;color:var(--warn);font-size:11px;font-weight:700;
                      border:1px solid var(--warn);padding:2px 6px}
.act-btn:disabled{opacity:.2;cursor:default;pointer-events:none;visibility:visible}
.move-btn{font-size:11px;padding:2px 4px}

.dim{opacity:.4}
.no-results{padding:48px 16px;text-align:center;color:var(--muted);font-size:14px}

footer{height:40px;background:var(--panel);border-top:1px solid var(--border);
       display:flex;align-items:center;padding:0 16px;gap:12px;flex-shrink:0}
#status-msg{font-size:12px;color:var(--muted)}
#status-msg.ok{color:#107c10}
#status-msg.err{color:var(--warn)}
#footer-count{font-size:12px;color:var(--muted)}

.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);
                z-index:100;align-items:flex-start;justify-content:center;padding-top:60px}
.modal-backdrop.open{display:flex}
.modal-card{background:#fff;border-radius:6px;box-shadow:0 8px 32px rgba(0,0,0,.28);
            width:480px;max-height:80vh;display:flex;flex-direction:column;overflow:hidden}
.modal-hdr{padding:12px 16px;border-bottom:1px solid var(--border);
           font-weight:600;font-size:14px;display:flex;align-items:center;gap:8px}
.modal-hdr span{flex:1}
.modal-x{background:none;border:none;font-size:18px;cursor:pointer;
          color:var(--muted);line-height:1;padding:2px 6px;flex-shrink:0}
.modal-x:hover{color:var(--text)}
.modal-body{padding:16px;overflow-y:auto;display:flex;flex-direction:column;gap:10px}
.field label{font-size:12px;font-weight:600;color:var(--muted);display:block;margin-bottom:3px}
.field .hint{font-weight:400;opacity:.75;font-size:11px}
.field input,.field textarea{width:100%;padding:7px 10px;border:1px solid #b3b3b3;
                              border-radius:4px;font-size:13px;font-family:inherit;
                              outline:none;resize:vertical}
.field input:focus,.field textarea:focus{
  border-color:var(--accent);box-shadow:0 0 0 2px rgba(0,120,212,.2)}
.field-row{display:flex;gap:6px;align-items:stretch}
.field-row input{flex:1;width:auto}
.pick-btn{padding:7px 10px;background:var(--accent-lt);color:var(--accent);
           border:1px solid var(--accent);border-radius:4px;cursor:pointer;
           font-size:13px;flex-shrink:0;white-space:nowrap;font-family:inherit}
.pick-btn:hover{background:var(--accent);color:#fff}
.ferr{color:var(--warn);font-size:12px;margin-top:3px;display:none}

/* workspace + slide-in editor panel */
.workspace{flex:1;display:flex;overflow:hidden}
.workspace>.body-area{flex:1;overflow:hidden;display:flex;flex-direction:column}
#ep{width:0;background:var(--panel);border-left:1px solid var(--border);
    display:flex;flex-direction:column;overflow:hidden;flex-shrink:0;
    transition:width .15s ease}
#ep.open{width:300px}
.ep-hdr{padding:8px 12px;border-bottom:1px solid var(--border);
        display:flex;align-items:center;gap:6px;flex-shrink:0;background:#f5f5f5}
.ep-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;
          flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted)}
.ep-x{background:none;border:none;cursor:pointer;color:var(--muted);font-size:15px;
       padding:1px 5px;line-height:1;border-radius:3px}
.ep-x:hover{color:var(--text);background:#e0e0e0}
.ep-body{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px}
.ep-err{color:var(--warn);font-size:12px;display:none;padding:3px 0}
.ep-ftr{padding:10px 12px;border-top:1px solid var(--border);
        display:flex;flex-direction:column;gap:6px;flex-shrink:0}
.ep-ftr-row{display:flex;gap:6px;align-items:center}
/* key recorder */
.rec-btn{padding:5px 9px;background:#e6f4ea;color:#107c10;border:1px solid #107c10;
         border-radius:4px;cursor:pointer;font-size:11px;flex-shrink:0;
         font-family:inherit;white-space:nowrap;line-height:1;transition:all .12s}
.rec-btn:hover{background:#107c10;color:#fff}
.rec-btn.recording{background:#fde8e8!important;color:var(--warn)!important;border-color:var(--warn)!important}
/* clickable + selected rows */
tbody tr.clickable{cursor:pointer}
tbody tr.selected td{background:#cce4ff!important}
tbody tr.user-row.selected td{background:#ffe099!important}
.modal-ftr{padding:12px 16px;border-top:1px solid var(--border);
           display:flex;justify-content:flex-end;gap:8px}
.btn-p{padding:7px 20px;background:var(--accent);color:#fff;border:none;
       border-radius:4px;cursor:pointer;font-size:13px;font-family:inherit;font-weight:600}
.btn-p:hover{background:var(--accent-dk)}
.btn-s{padding:7px 16px;background:var(--bg);color:var(--text);
       border:1px solid var(--border);border-radius:4px;cursor:pointer;font-size:13px;font-family:inherit}
.btn-s:hover{background:#e8e8e8}
pre.ctx-pre{font-size:12px;white-space:pre-wrap;overflow:auto;max-height:400px;
            background:#f8f8f8;padding:10px;border-radius:4px;border:1px solid var(--border);
            font-family:"Cascadia Code","Consolas","Courier New",monospace}

.picker-card{width:560px}
.picker-search{flex:1;padding:6px 10px;border:1px solid #b3b3b3;border-radius:4px;
               font-size:13px;font-family:inherit;outline:none}
.picker-search:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(0,120,212,.2)}
.picker-list{overflow-y:auto;flex:1;max-height:420px}
.picker-group{padding:4px 12px 2px;font-size:10px;font-weight:700;text-transform:uppercase;
              letter-spacing:.5px;color:var(--muted);background:#f8f8f8;
              border-bottom:1px solid var(--border-lt);position:sticky;top:0}
.picker-item{padding:6px 12px;cursor:pointer;display:flex;align-items:baseline;
             gap:8px;border-bottom:1px solid var(--border-lt)}
.picker-item:hover{background:var(--accent-lt)}
.picker-cmd{font-family:"Cascadia Code","Consolas","Courier New",monospace;
            font-size:12px;flex-shrink:0;color:var(--text)}
.picker-cap{color:var(--muted);font-size:11px;flex:1;
            white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.picker-src{font-size:10px;color:#aaa;flex-shrink:0}
.picker-empty{padding:32px;text-align:center;color:var(--muted)}
</style>
</head>
<body>

<header>
  <h1>&#9881; ST Config</h1>
  <div class="search-wrap">
    <svg width="14" height="14" viewBox="0 0 16 16" fill="white">
      <path d="M11.742 10.344a6.5 6.5 0 1 0-1.397 1.398h-.001c.03.04.062.078.098.115l3.85 3.85a1 1 0 0 0 1.415-1.414l-3.85-3.85a1.007 1.007 0 0 0-.115-.099zm-5.242 1.656a5.5 5.5 0 1 1 0-11 5.5 5.5 0 0 1 0 11z"/>
    </svg>
    <input type="search" id="search" placeholder="Filter&#8230;" oninput="onSearch()">
  </div>
  <div class="spacer"></div>
  <button id="btn-add-sep" class="hdr-btn" onclick="addSep()" style="display:none">&#43; Separator</button>
  <button id="btn-add" class="hdr-btn primary" onclick="openAddModal()">&#43; Add</button>
  <button class="hdr-btn" onclick="closeApp()">&#10005;&#160;Close</button>
</header>

<div class="tab-bar">
  <div class="tab active" data-tab="keys"  onclick="setTab('keys')">&#9000; Keys</div>
  <div class="tab"        data-tab="cmds"  onclick="setTab('cmds')">&#8984; Commands</div>
  <div class="tab"        data-tab="menus" onclick="setTab('menus')">&#9776; Menus</div>
</div>

<div class="workspace">
<div class="body-area">

  <div class="tab-section" id="tab-keys">
    <div class="filter-bar">
      <div class="chip active" data-kf="all"       onclick="kbSetFilter('all')">All <span class="cnt" id="kb-cnt-all">0</span></div>
      <div class="chip"        data-kf="user"      onclick="kbSetFilter('user')">User <span class="cnt" id="kb-cnt-user">0</span></div>
      <div class="chip warn"   data-kf="conflicts" onclick="kbSetFilter('conflicts')">Conflicts <span class="cnt" id="kb-cnt-conf">0</span></div>
    </div>
    <main>
      <table>
        <thead><tr>
          <th style="width:220px">Keys</th>
          <th style="width:210px">Command</th>
          <th>Args</th>
          <th style="width:52px;text-align:center">Ctx</th>
          <th style="width:120px">Source</th>
          <th style="width:136px"></th>
        </tr></thead>
        <tbody id="tb-keys"></tbody>
      </table>
      <div class="no-results" id="nr-keys" style="display:none">No matching bindings.</div>
    </main>
  </div>

  <div class="tab-section hidden" id="tab-cmds">
    <div class="filter-bar">
      <div class="chip active" data-cf="all"  onclick="cmdsSetFilter('all')">All <span class="cnt" id="cm-cnt-all">0</span></div>
      <div class="chip"        data-cf="user" onclick="cmdsSetFilter('user')">User <span class="cnt" id="cm-cnt-user">0</span></div>
    </div>
    <main>
      <table>
        <thead><tr>
          <th style="width:260px">Caption</th>
          <th style="width:210px">Command</th>
          <th>Args</th>
          <th style="width:120px">Source</th>
          <th style="width:136px"></th>
        </tr></thead>
        <tbody id="tb-cmds"></tbody>
      </table>
      <div class="no-results" id="nr-cmds" style="display:none">No matching commands.</div>
    </main>
  </div>

  <div class="tab-section hidden" id="tab-menus">
    <div class="filter-bar" id="menu-chips"></div>
    <main>
      <table>
        <thead><tr>
          <th style="width:280px">Caption</th>
          <th style="width:220px">Command</th>
          <th>Args</th>
          <th style="width:100px"></th>
        </tr></thead>
        <tbody id="tb-menus"></tbody>
      </table>
      <div class="no-results" id="nr-menus" style="display:none">No items in this menu. Click + Add to create one.</div>
    </main>
  </div>

</div>

<!-- Editor panel -->
<div id="ep">
  <div class="ep-hdr">
    <span class="ep-title" id="ep-title">Edit</span>
    <button class="ep-x" onclick="closePanel()" title="Close">&#10005;</button>
  </div>
  <div class="ep-body">
    <div class="field" id="epf-keys" style="display:none">
      <label>Keys <span class="hint">&#8212; chord: record twice</span></label>
      <div class="field-row">
        <input type="text" id="ep-keys" autocomplete="off" spellcheck="false" placeholder="e.g. ctrl+s">
        <button class="rec-btn" id="ep-rec" onclick="toggleRecord()" type="button">&#9210; Rec</button>
      </div>
    </div>
    <div class="field" id="epf-cap" style="display:none">
      <label>Caption</label>
      <input type="text" id="ep-cap" autocomplete="off">
    </div>
    <div class="field" id="epf-cmd">
      <label>Command</label>
      <div class="field-row">
        <input type="text" id="ep-cmd" autocomplete="off" spellcheck="false">
        <button class="pick-btn" id="ep-pick" onclick="openCmdPickerFor('panel')" type="button" style="font-size:11px;padding:5px 8px">&#8981; Pick</button>
      </div>
    </div>
    <div class="field">
      <label>Args <span class="hint">&#8212; JSON</span></label>
      <textarea id="ep-args" rows="2" spellcheck="false"></textarea>
    </div>
    <div class="field" id="epf-ctx" style="display:none">
      <label>Context <span class="hint">&#8212; JSON array</span></label>
      <textarea id="ep-ctx" rows="2" spellcheck="false"></textarea>
    </div>
    <div class="ep-err" id="ep-err"></div>
  </div>
  <div class="ep-ftr">
    <div class="ep-ftr-row">
      <button id="ep-b-del"      class="act-btn del" onclick="doDelete(this)" style="display:none">&#128465;</button>
      <button id="ep-b-close"    class="btn-s"       onclick="closePanel()"   style="display:none">Close</button>
      <button id="ep-b-discard"  class="btn-s"       onclick="closePanel()"   style="display:none">Discard</button>
      <div style="flex:1"></div>
      <button id="ep-b-override" class="btn-s"       onclick="panelOverride()" style="display:none">&#8853; Override</button>
      <button id="ep-b-apply"    class="btn-p"       onclick="panelApply()"    style="display:none">Apply</button>
    </div>
    <div id="ep-ftr-row2" style="display:none">
      <button class="btn-s" style="width:100%;margin-top:2px" onclick="panelBind()">&#9000; Add Keybinding</button>
    </div>
  </div>
</div>

</div>

<footer>
  <span id="status-msg">Ready</span>
  <div class="spacer"></div>
  <span id="footer-count"></span>
</footer>

<!-- Add/Edit modal -->
<div class="modal-backdrop" id="edit-modal" onclick="if(event.target===this)closeModal()">
  <div class="modal-card">
    <div class="modal-hdr">
      <span id="modal-title">Add</span>
      <button class="modal-x" onclick="closeModal()">&#10005;</button>
    </div>
    <div class="modal-body">
      <div class="field" id="mf-keys">
        <label>Keys <span class="hint">&#8212; e.g. <code>ctrl+s</code> &nbsp; chord: <code>ctrl+k,ctrl+b</code></span></label>
        <input type="text" id="m-keys" autocomplete="off" spellcheck="false">
        <div class="ferr" id="err-keys"></div>
      </div>
      <div class="field" id="mf-caption">
        <label>Caption</label>
        <input type="text" id="m-caption" autocomplete="off">
        <div class="ferr" id="err-caption"></div>
      </div>
      <div class="field">
        <label>Command</label>
        <div class="field-row">
          <input type="text" id="m-cmd" autocomplete="off" spellcheck="false">
          <button class="pick-btn" onclick="openCmdPicker()" title="Browse all commands">&#8981; Pick</button>
        </div>
        <div class="ferr" id="err-cmd"></div>
      </div>
      <div class="field">
        <label>Args <span class="hint">&#8212; optional JSON object</span></label>
        <textarea id="m-args" rows="2" spellcheck="false"></textarea>
        <div class="ferr" id="err-args"></div>
      </div>
      <div class="field" id="mf-context">
        <label>Context <span class="hint">&#8212; optional JSON array</span></label>
        <textarea id="m-context" rows="3" spellcheck="false"></textarea>
        <div class="ferr" id="err-ctx"></div>
      </div>
    </div>
    <div class="modal-ftr">
      <button class="btn-s" onclick="closeModal()">Cancel</button>
      <button class="btn-p" onclick="saveModal()">Save</button>
    </div>
  </div>
</div>

<!-- Command picker modal -->
<div class="modal-backdrop" id="picker-modal" onclick="if(event.target===this)closeCmdPicker()">
  <div class="modal-card picker-card">
    <div class="modal-hdr">
      <span>Pick Command</span>
      <input class="picker-search" id="picker-search" type="search"
             placeholder="Filter commands&#8230;" oninput="filterPicker(this.value)" autocomplete="off">
      <button class="modal-x" onclick="closeCmdPicker()">&#10005;</button>
    </div>
    <div class="picker-list" id="picker-list"></div>
  </div>
</div>

<!-- Context viewer -->
<div class="modal-backdrop" id="ctx-modal" onclick="if(event.target===this)closeCtxModal()">
  <div class="modal-card" style="width:500px">
    <div class="modal-hdr">
      <span>Context</span>
      <button class="modal-x" onclick="closeCtxModal()">&#10005;</button>
    </div>
    <div class="modal-body">
      <pre class="ctx-pre" id="ctx-content"></pre>
    </div>
  </div>
</div>

<script>
const D = __DATA__;
let _tab = 'keys', _search = '';
let _kbFilter = 'all', _cmdFilter = 'all', _menuFilter = 'all';
let _editIdx = null, _editType = 'kb';
let _delPending = null;

// ── Utilities ─────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function renderKeys(keys) {
  if (!Array.isArray(keys)) return esc(String(keys));
  return keys.map((k, i) => {
    const pre = i > 0 ? '<span class="chord-arr">&#8594;</span>' : '';
    const kbds = k.split('+').map((p, j) =>
      (j > 0 ? '<span class="plus">+</span>' : '') + `<kbd>${esc(p)}</kbd>`).join('');
    return pre + kbds;
  }).join('');
}

function shortJson(v) {
  if (v == null) return '';
  const s = JSON.stringify(v);
  return esc(s.length > 36 ? s.slice(0, 34) + '&#8230;' : s);
}

function setStatus(msg, cls) {
  const el = document.getElementById('status-msg');
  el.textContent = msg; el.className = cls || '';
  if (cls === 'ok') setTimeout(() => { if (el.textContent === msg) { el.textContent = 'Ready'; el.className = ''; } }, 2500);
}

function setFooter(t) { document.getElementById('footer-count').textContent = t; }
function closeApp()   { fetch('/close').finally(() => window.close()); }

// ── API ───────────────────────────────────────────────────────────────────────

function api(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  }).then(r => r.json()).then(r => {
    if (!r.ok) throw new Error(r.error || 'unknown');
    if (r.data) Object.assign(D, r.data);
    return r;
  });
}

// ── Tab switching ─────────────────────────────────────────────────────────────

function setTab(name) {
  closePanel();
  _tab = name;
  document.querySelectorAll('.tab').forEach(el =>
    el.classList.toggle('active', el.dataset.tab === name));
  document.querySelectorAll('.tab-section').forEach(el =>
    el.classList.toggle('hidden', el.id !== 'tab-' + name));
  document.getElementById('btn-add').style.display     = '';
  document.getElementById('btn-add-sep').style.display = (name === 'cmds' || name === 'menus') ? '' : 'none';
  document.getElementById('search').value = '';
  _search = '';
  if (name === 'keys')  kbApply();
  if (name === 'cmds')  cmdsApply();
  if (name === 'menus') menusApply();
}

function onSearch() {
  _search = document.getElementById('search').value.toLowerCase().trim();
  if (_tab === 'keys')  kbApply();
  if (_tab === 'cmds')  cmdsApply();
  if (_tab === 'menus') menusApply();
}

// ── Keys ──────────────────────────────────────────────────────────────────────

function kbBuild() {
  closePanel();
  const tbody = document.getElementById('tb-keys');
  tbody.innerHTML = '';
  let ui = -1;
  D.kb.bindings.forEach((b, i) => {
    if (b.is_user) ui++;
    const tr = document.createElement('tr');
    tr.dataset.user     = b.is_user ? '1' : '0';
    tr.dataset.conflict = b.conflict ? '1' : '0';
    tr.dataset.ks       = b.key_str.toLowerCase();
    tr.dataset.cmd      = (b.command || '').toLowerCase();
    tr.dataset.src      = (b.source || '').toLowerCase();
    tr.dataset.di       = i;
    if (b.is_user) { tr.classList.add('user-row'); tr.dataset.ui = ui; }
    if (b.conflict) tr.classList.add('conflict');

    if (b.is_sep && b.is_user) {
      tr.classList.add('sep-row');
      tr.innerHTML =
        `<td colspan="5"><div class="sep-inner"><div class="sep-line"></div><span class="sep-label">separator</span><div class="sep-line"></div></div></td>` +
        `<td class="act-cell">${_moveButtons('kb', ui)}${_delButton('kb', ui)}</td>`;
      tbody.appendChild(tr);
      return;
    }
    if (b.is_sep) return;

    tr.classList.add('clickable');
    tr.onclick = e => { if (e.target.closest('button')) return; openPanel('kb', i, b.is_user ? ui : -1); };

    const ctxCell = b.context != null
      ? `<button class="ctx-btn" onclick="showCtx(${i})">ctx</button>`
      : '<span style="color:#ddd">&#8212;</span>';
    const act = b.is_user
      ? _moveButtons('kb', ui) + _delButton('kb', ui)
      : `<button class="act-btn" style="font-size:11px;color:var(--accent)" title="Override — copy to User" onclick="openPanel('kb',${i},-1)">&#8853;</button>`;
    tr.innerHTML =
      `<td><span class="key-seq">${renderKeys(b.keys)}</span></td>` +
      `<td class="mono">${esc(b.command)}</td>` +
      `<td class="muted-sm trunc" title="${esc(JSON.stringify(b.args??''))}">${shortJson(b.args)}</td>` +
      `<td style="text-align:center">${ctxCell}</td>` +
      `<td class="src${b.is_user?' is-user':''}">${esc(b.source)}</td>` +
      `<td class="act-cell">${act}</td>`;
    tbody.appendChild(tr);
  });

  _fixMoveBtns('#tb-keys');

  const rows = tbody.querySelectorAll('tr');
  let u = 0, c = 0;
  rows.forEach(tr => { if (tr.dataset.user==='1') u++; if (tr.dataset.conflict==='1') c++; });
  document.getElementById('kb-cnt-all').textContent  = rows.length;
  document.getElementById('kb-cnt-user').textContent = u;
  document.getElementById('kb-cnt-conf').textContent = c;
  kbApply();
}

function kbSetFilter(f) {
  _kbFilter = f;
  document.querySelectorAll('[data-kf]').forEach(el =>
    el.classList.toggle('active', el.dataset.kf === f));
  kbApply();
}

function kbApply() {
  const rows = document.querySelectorAll('#tb-keys tr');
  let vis = 0;
  rows.forEach(tr => {
    const isSep = tr.classList.contains('sep-row');
    const ok = (_kbFilter==='all' || (_kbFilter==='user' && tr.dataset.user==='1') ||
                (_kbFilter==='conflicts' && tr.dataset.conflict==='1')) &&
               (isSep || !_search ||
                tr.dataset.ks.includes(_search) || tr.dataset.cmd.includes(_search) ||
                tr.dataset.src.includes(_search));
    tr.classList.toggle('hidden', !ok);
    if (ok && !isSep) vis++;
  });
  document.getElementById('nr-keys').style.display = vis ? 'none' : '';
  const total = [...rows].filter(tr => !tr.classList.contains('sep-row')).length;
  setFooter(vis < total ? `${vis} of ${total} bindings` : `${total} bindings`);
}

// ── Commands ──────────────────────────────────────────────────────────────────

function cmdsBuild() {
  closePanel();
  const tbody = document.getElementById('tb-cmds');
  tbody.innerHTML = '';
  let ui = -1;
  D.cmds.bindings.forEach((c, i) => {
    if (c.is_user) ui++;
    const tr = document.createElement('tr');
    tr.dataset.cap  = (c.caption || '').toLowerCase();
    tr.dataset.cmd  = (c.command || '').toLowerCase();
    tr.dataset.src  = (c.source || '').toLowerCase();
    tr.dataset.user = c.is_user ? '1' : '0';
    tr.dataset.di   = i;
    if (c.is_user) { tr.classList.add('user-row'); tr.dataset.ui = ui; }

    if (c.is_sep && c.is_user) {
      tr.classList.add('sep-row');
      tr.innerHTML =
        `<td colspan="4"><div class="sep-inner"><div class="sep-line"></div><span class="sep-label">separator</span><div class="sep-line"></div></div></td>` +
        `<td class="act-cell">${_moveButtons('cmd', ui)}${_delButton('cmd', ui)}</td>`;
      tbody.appendChild(tr);
      return;
    }
    if (c.is_sep) return;

    tr.classList.add('clickable');
    tr.onclick = e => { if (e.target.closest('button')) return; openPanel('cmd', i, c.is_user ? ui : -1); };

    const act = c.is_user
      ? _moveButtons('cmd', ui) + _delButton('cmd', ui)
      : `<button class="act-btn" style="font-size:11px;color:#107c10" title="Add keybinding for this command" onclick="openPanel('cmd',${i},-1)">&#9000;</button>`;
    tr.innerHTML =
      `<td>${esc(c.caption)}</td>` +
      `<td class="mono">${esc(c.command)}</td>` +
      `<td class="muted-sm trunc" title="${esc(JSON.stringify(c.args??''))}">${shortJson(c.args)}</td>` +
      `<td class="src${c.is_user?' is-user':''}">${esc(c.source)}</td>` +
      `<td class="act-cell">${act}</td>`;
    tbody.appendChild(tr);
  });

  _fixMoveBtns('#tb-cmds');

  const rows = tbody.querySelectorAll('tr');
  let u = 0;
  rows.forEach(tr => { if (tr.dataset.user==='1') u++; });
  document.getElementById('cm-cnt-all').textContent  = rows.length;
  document.getElementById('cm-cnt-user').textContent = u;
  cmdsApply();
}

function cmdsSetFilter(f) {
  _cmdFilter = f;
  document.querySelectorAll('[data-cf]').forEach(el =>
    el.classList.toggle('active', el.dataset.cf === f));
  cmdsApply();
}

function cmdsApply() {
  const rows = document.querySelectorAll('#tb-cmds tr');
  let vis = 0;
  rows.forEach(tr => {
    const isSep = tr.classList.contains('sep-row');
    const ok = (_cmdFilter==='all' || (_cmdFilter==='user' && tr.dataset.user==='1')) &&
               (isSep || !_search ||
                tr.dataset.cap.includes(_search) || tr.dataset.cmd.includes(_search) ||
                tr.dataset.src.includes(_search));
    tr.classList.toggle('hidden', !ok);
    if (ok && !isSep) vis++;
  });
  document.getElementById('nr-cmds').style.display = vis ? 'none' : '';
  const total = [...rows].filter(tr => !tr.classList.contains('sep-row')).length;
  setFooter(vis < total ? `${vis} of ${total} commands` : `${total} commands`);
}

// ── Menus ─────────────────────────────────────────────────────────────────────

const _ALL_MENUS = ['Side Bar','Tab Context','Context','Main','Find in Files','Widget Context'];
let _currentMenu = null;

function menusBuild() {
  closePanel();
  const bar = document.getElementById('menu-chips');
  bar.innerHTML = '';
  const userKeys = Object.keys(D.user_menus);
  const shown = [...new Set([...userKeys, ..._ALL_MENUS])];
  if (!_currentMenu || !shown.includes(_currentMenu)) _currentMenu = shown[0] || null;
  shown.forEach(name => {
    const cnt = (D.user_menus[name] || []).length;
    const el = document.createElement('div');
    el.className = 'chip' + (name === _currentMenu ? ' active' : '');
    el.dataset.mn = name;
    el.onclick = () => menusSetFilter(name);
    el.innerHTML = `${esc(name)} <span class="cnt">${cnt}</span>`;
    bar.appendChild(el);
  });
  menusRender();
}

function menusSetFilter(name) {
  _currentMenu = name;
  document.querySelectorAll('[data-mn]').forEach(el =>
    el.classList.toggle('active', el.dataset.mn === name));
  menusRender();
}

function menusRender() {
  closePanel();
  const tbody = document.getElementById('tb-menus');
  tbody.innerHTML = '';
  const items = (_currentMenu && D.user_menus[_currentMenu]) || [];
  items.forEach((item, i) => {
    const tr = document.createElement('tr');
    tr.dataset.di  = i;
    tr.dataset.cap = (item.caption || '').toLowerCase();
    tr.dataset.cmd = (item.command || '').toLowerCase();
    const isSep = item.caption === '-';
    if (isSep) {
      tr.classList.add('sep-row','user-row');
      tr.innerHTML =
        `<td colspan="3"><div class="sep-inner"><div class="sep-line"></div><span class="sep-label">separator</span><div class="sep-line"></div></div></td>` +
        `<td class="act-cell">${_moveButtons('menu', i)}<button class="act-btn del" data-del-type="menu" data-del-idx="${i}" onclick="doDelete(this)">&#128465;</button></td>`;
    } else {
      tr.classList.add('user-row','clickable');
      tr.onclick = e => { if (e.target.closest('button')) return; openPanel('menu', i, i); };
      tr.innerHTML =
        `<td>${esc(item.caption || '')}</td>` +
        `<td class="mono">${esc(item.command || '')}</td>` +
        `<td class="muted-sm trunc" title="${esc(JSON.stringify(item.args??''))}">${shortJson(item.args)}</td>` +
        `<td class="act-cell">${_moveButtons('menu', i)}<button class="act-btn del" data-del-type="menu" data-del-idx="${i}" onclick="doDelete(this)">&#128465;</button></td>`;
    }
    tbody.appendChild(tr);
  });
  const rows = document.querySelectorAll('#tb-menus tr');
  if (rows.length > 0) {
    rows[0].querySelector('.up-btn')?.setAttribute('disabled','');
    rows[rows.length-1].querySelector('.dn-btn')?.setAttribute('disabled','');
  }
  document.getElementById('nr-menus').style.display = items.length ? 'none' : '';
  setFooter(`${items.length} item${items.length!==1?'s':''} in ${_currentMenu||''}`);
}

function menusApply() { menusRender(); }

function menuMove(i, dir) {
  api('/menu/move', {menu: _currentMenu, index: i, direction: dir})
    .then(() => menusBuild())
    .catch(e => setStatus('Error: ' + e.message, 'err'));
}

// ── Move buttons helper ───────────────────────────────────────────────────────

function _moveButtons(type, ui) {
  return `<button class="act-btn move-btn up-btn" title="Move up"   onclick="${type}Move(${ui},'up')">&#8593;</button>` +
         `<button class="act-btn move-btn dn-btn" title="Move down" onclick="${type}Move(${ui},'down')">&#8595;</button>`;
}

function _delButton(type, ui) {
  return `<button class="act-btn del" title="Delete" data-del-type="${type}" data-del-idx="${ui}" onclick="doDelete(this)">&#128465;</button>`;
}

function _fixMoveBtns(selector) {
  const userRows = [...document.querySelectorAll(selector + ' tr[data-user="1"]')];
  userRows.forEach((tr, i) => {
    if (i === 0) tr.querySelector('.up-btn')?.setAttribute('disabled', '');
    if (i === userRows.length - 1) tr.querySelector('.dn-btn')?.setAttribute('disabled', '');
  });
}

function kbMove(ui, dir) {
  api('/kb/move', {index: ui, direction: dir})
    .then(() => kbBuild())
    .catch(e => setStatus('Error: ' + e.message, 'err'));
}

function cmdMove(ui, dir) {
  api('/cmd/move', {index: ui, direction: dir})
    .then(() => cmdsBuild())
    .catch(e => setStatus('Error: ' + e.message, 'err'));
}

// ── Separators ────────────────────────────────────────────────────────────────

function addSep() {
  if (_tab === 'menus') {
    if (!_currentMenu) return;
    api('/menu/add', {menu: _currentMenu, entry: {caption: '-'}})
      .then(() => { menusBuild(); setStatus('Separator added', 'ok'); })
      .catch(e => setStatus('Error: ' + e.message, 'err'));
  } else {
    api('/cmd/add', {entry: {caption: '-'}})
      .then(() => { cmdsBuild(); setStatus('Separator added', 'ok'); })
      .catch(e => setStatus('Error: ' + e.message, 'err'));
  }
}

// ── Delete (two-click) ────────────────────────────────────────────────────────

function doDelete(btn) {
  if (_delPending && _delPending.btn === btn) {
    clearTimeout(_delPending.timer);
    const {type, idx} = _delPending;
    _resetDel();
    const delBody = type === 'menu' ? {menu: _currentMenu, index: idx} : {index: idx};
    api('/' + type + '/delete', delBody)
      .then(() => { setStatus('Deleted', 'ok'); if(type==='kb') kbBuild(); else if(type==='cmd') cmdsBuild(); else menusBuild(); })
      .catch(e => setStatus('Error: ' + e.message, 'err'));
  } else {
    _resetDel();
    btn.textContent = 'Sure?';
    btn.classList.add('del-confirm');
    _delPending = {
      btn, type: btn.dataset.delType, idx: +btn.dataset.delIdx,
      timer: setTimeout(_resetDel, 3000)
    };
  }
}

function _resetDel() {
  if (_delPending?.btn) {
    _delPending.btn.innerHTML = '&#128465;';
    _delPending.btn.classList.remove('del-confirm');
  }
  if (_delPending?.timer) clearTimeout(_delPending.timer);
  _delPending = null;
}

document.addEventListener('click', e => {
  if (_delPending && !e.target.classList.contains('del')) _resetDel();
});

// ── Context viewer ────────────────────────────────────────────────────────────

function showCtx(i) {
  document.getElementById('ctx-content').textContent =
    JSON.stringify(D.kb.bindings[i].context, null, 2);
  document.getElementById('ctx-modal').classList.add('open');
}
function closeCtxModal() { document.getElementById('ctx-modal').classList.remove('open'); }

// ── Add / Edit modal ──────────────────────────────────────────────────────────

function openAddModal(forceType) {
  _editIdx = null;
  _editType = forceType || (_tab === 'cmds' ? 'cmd' : _tab === 'menus' ? 'menu' : 'kb');
  document.getElementById('modal-title').textContent =
    _editType === 'kb' ? 'Add Binding' : 'Add Command';
  ['m-keys','m-caption','m-cmd','m-args','m-context'].forEach(id =>
    document.getElementById(id).value = '');
  _setModalMode();
  clearErrors();
  document.getElementById('edit-modal').classList.add('open');
  setTimeout(() => document.getElementById(_editType==='kb' ? 'm-keys' : 'm-caption').focus(), 50);
}

function openEditModal(type, ui) {
  _editIdx = ui; _editType = type;
  const e = (type === 'kb' ? D.kb : D.cmds).user_entries[ui];
  document.getElementById('modal-title').textContent =
    type === 'kb' ? 'Edit Binding' : 'Edit Command';
  document.getElementById('m-keys').value    = type==='kb' ? (e.keys||[]).join(',') : '';
  document.getElementById('m-caption').value = e.caption || '';
  document.getElementById('m-cmd').value     = e.command || '';
  document.getElementById('m-args').value    = e.args != null ? JSON.stringify(e.args, null, 2) : '';
  document.getElementById('m-context').value = type==='kb' && e.context != null
    ? JSON.stringify(e.context, null, 2) : '';
  _setModalMode();
  clearErrors();
  document.getElementById('edit-modal').classList.add('open');
  setTimeout(() => document.getElementById(type==='kb' ? 'm-keys' : 'm-caption').focus(), 50);
}

function _setModalMode() {
  const isKb = _editType === 'kb';
  document.getElementById('mf-keys').style.display    = isKb ? '' : 'none';
  document.getElementById('mf-caption').style.display = isKb ? 'none' : '';
  document.getElementById('mf-context').style.display = isKb ? '' : 'none';
  document.getElementById('modal-title').textContent  =
    _editIdx === null
      ? (_editType === 'kb' ? 'Add Binding' : _editType === 'menu' ? 'Add Menu Item' : 'Add Command')
      : (_editType === 'kb' ? 'Edit Binding' : _editType === 'menu' ? 'Edit Menu Item' : 'Edit Command');
}

function closeModal() { document.getElementById('edit-modal').classList.remove('open'); }

function clearErrors() {
  ['err-keys','err-caption','err-cmd','err-args','err-ctx'].forEach(id => {
    const el = document.getElementById(id);
    el.style.display = 'none'; el.textContent = '';
  });
}

function showErr(id, msg) {
  const el = document.getElementById(id);
  el.textContent = msg; el.style.display = '';
}

function saveModal() {
  clearErrors();
  const isKb    = _editType === 'kb';
  const rawKeys = document.getElementById('m-keys').value.trim();
  const rawCap  = document.getElementById('m-caption').value.trim();
  const rawCmd  = document.getElementById('m-cmd').value.trim();
  const rawArgs = document.getElementById('m-args').value.trim();
  const rawCtx  = document.getElementById('m-context').value.trim();
  let valid = true;
  if ( isKb && !rawKeys) { showErr('err-keys',    'Required.'); valid = false; }
  if (!isKb && !rawCap)  { showErr('err-caption', 'Required.'); valid = false; }
  if (!rawCmd)            { showErr('err-cmd',     'Required.'); valid = false; }
  let args = null, context = null;
  if (rawArgs) { try { args = JSON.parse(rawArgs); } catch(e) { showErr('err-args','Invalid JSON: '+e.message); valid=false; } }
  if (isKb && rawCtx) { try { context = JSON.parse(rawCtx); } catch(e) { showErr('err-ctx','Invalid JSON: '+e.message); valid=false; } }
  if (!valid) return;

  let entry;
  if (isKb) {
    entry = {keys: rawKeys.split(',').map(s=>s.trim()).filter(Boolean), command: rawCmd};
    if (args    != null) entry.args    = args;
    if (context != null) entry.context = context;
  } else {
    entry = {caption: rawCap, command: rawCmd};
    if (args != null) entry.args = args;
  }

  const isMenu = _editType === 'menu';
  const base = isKb ? '/kb' : isMenu ? '/menu' : '/cmd';
  const mkBody = (extra) => isMenu ? {menu: _currentMenu, ...extra} : extra;
  const [url, body] = _editIdx === null
    ? [base+'/add',  mkBody({entry})]
    : [base+'/edit', mkBody({index: _editIdx, entry})];

  api(url, body).then(() => {
    closeModal();
    setStatus('Saved', 'ok');
    if (isKb) kbBuild(); else if (isMenu) menusBuild(); else cmdsBuild();
  }).catch(e => setStatus('Error: ' + e.message, 'err'));
}

// ── Command picker ────────────────────────────────────────────────────────────

let _allPickerItems = null;

function _buildPickerItems() {
  if (_allPickerItems) return _allPickerItems;
  const seen = new Map();
  D.cmds.bindings.forEach(c => {
    if (c.is_sep || !c.command) return;
    if (!seen.has(c.command)) seen.set(c.command, {command: c.command, caption: c.caption, source: c.source, args: c.args});
    else if (!seen.get(c.command).caption && c.caption) seen.get(c.command).caption = c.caption;
  });
  D.kb.bindings.forEach(b => {
    if (b.is_sep || !b.command || seen.has(b.command)) return;
    seen.set(b.command, {command: b.command, caption: '', source: b.source, args: b.args});
  });
  _allPickerItems = [...seen.values()].sort((a,b) => a.command.localeCompare(b.command));
  return _allPickerItems;
}

function openCmdPicker() {
  _buildPickerItems();
  filterPicker('');
  document.getElementById('picker-search').value = '';
  document.getElementById('picker-modal').classList.add('open');
  setTimeout(() => document.getElementById('picker-search').focus(), 50);
}

function closeCmdPicker() {
  document.getElementById('picker-modal').classList.remove('open');
}

function filterPicker(q) {
  q = q.toLowerCase().trim();
  const items = _buildPickerItems();
  const filtered = q ? items.filter(i =>
    i.command.toLowerCase().includes(q) ||
    (i.caption && i.caption.toLowerCase().includes(q)) ||
    i.source.toLowerCase().includes(q)
  ) : items;

  const list = document.getElementById('picker-list');
  list.innerHTML = '';

  if (!filtered.length) {
    list.innerHTML = '<div class="picker-empty">No matches</div>';
    return;
  }

  let lastSrc = null;
  filtered.forEach(item => {
    if (item.source !== lastSrc) {
      lastSrc = item.source;
      const g = document.createElement('div');
      g.className = 'picker-group';
      g.textContent = item.source;
      list.appendChild(g);
    }
    const div = document.createElement('div');
    div.className = 'picker-item';
    div.innerHTML =
      `<span class="picker-cmd">${esc(item.command)}</span>` +
      (item.caption ? `<span class="picker-cap">${esc(item.caption)}</span>` : '<span class="picker-cap"></span>') +
      `<span class="picker-src">${esc(item.source)}</span>`;
    div.onclick = () => pickCmd(item);
    list.appendChild(div);
  });
}

function pickCmd(item) {
  const cmdId = _pickerForPanel ? 'ep-cmd' : 'm-cmd';
  const argsId = _pickerForPanel ? 'ep-args' : 'm-args';
  _pickerForPanel = false;
  document.getElementById(cmdId).value = item.command;
  const argsEl = document.getElementById(argsId);
  if (argsEl && !argsEl.value.trim() && item.args != null) {
    argsEl.value = JSON.stringify(item.args, null, 2);
  }
  closeCmdPicker();
  document.getElementById(cmdId).focus();
}

// ── Editor Panel ──────────────────────────────────────────────────────────────

let _epState = {type: null, dataIdx: -1, ui: -1};
let _pickerForPanel = false;

function openPanel(type, dataIdx, ui) {
  stopRecord();
  document.getElementById('ep-err').style.display = 'none';
  let data;
  if      (type === 'kb')   data = D.kb.bindings[dataIdx];
  else if (type === 'cmd')  data = D.cmds.bindings[dataIdx];
  else                      data = (D.user_menus[_currentMenu] || [])[dataIdx];
  _epState = {type, dataIdx, ui};
  const isKb   = type === 'kb';
  const isMenu = type === 'menu';
  const isUser = isMenu ? true : ui >= 0;

  document.getElementById('ep-title').textContent =
    isMenu ? (_currentMenu || 'Menu') : (data.source || (isUser ? 'User' : 'System'));
  document.getElementById('epf-keys').style.display = isKb   ? '' : 'none';
  document.getElementById('epf-cap').style.display  = isKb   ? 'none' : '';
  document.getElementById('epf-ctx').style.display  = isKb   ? '' : 'none';

  if (isKb) {
    document.getElementById('ep-keys').value = (data.keys || []).join(',');
    document.getElementById('ep-ctx').value  = data.context != null ? JSON.stringify(data.context, null, 2) : '';
  } else {
    document.getElementById('ep-cap').value = data.caption || '';
  }
  document.getElementById('ep-cmd').value  = data.command || '';
  document.getElementById('ep-args').value = data.args != null ? JSON.stringify(data.args, null, 2) : '';

  ['ep-keys','ep-cap','ep-cmd','ep-args','ep-ctx'].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.readOnly = !isUser; el.style.background = isUser ? '' : '#f8f8f8'; }
  });
  document.getElementById('ep-rec').style.display  = (isKb && isUser) ? '' : 'none';
  document.getElementById('ep-pick').style.display = isUser ? '' : 'none';

  const showB = (id, v) => { document.getElementById(id).style.display = v ? '' : 'none'; };
  showB('ep-b-del',      isUser);
  showB('ep-b-close',    !isUser);
  showB('ep-b-discard',  isUser);
  showB('ep-b-override', !isUser && isKb);
  showB('ep-b-apply',    isUser);
  document.getElementById('ep-ftr-row2').style.display = (!isKb && !isMenu) ? '' : 'none';

  if (isUser) {
    document.getElementById('ep-b-del').dataset.delType = type;
    document.getElementById('ep-b-del').dataset.delIdx  = ui;
  }

  document.querySelectorAll('tbody tr.selected').forEach(r => r.classList.remove('selected'));
  document.querySelectorAll('#tb-' + (isKb ? 'keys' : 'cmds') + ' tr[data-di="' + dataIdx + '"]')
    .forEach(r => r.classList.add('selected'));

  document.getElementById('ep').classList.add('open');
  if (isUser) setTimeout(() => {
    const fld = document.getElementById(isKb ? 'ep-keys' : 'ep-cap');
    if (fld) fld.focus();
  }, 100);
}

function closePanel() {
  stopRecord();
  _epState = {type: null, dataIdx: -1, ui: -1};
  document.getElementById('ep').classList.remove('open');
  document.querySelectorAll('tbody tr.selected').forEach(r => r.classList.remove('selected'));
}

function panelApply() {
  const err = document.getElementById('ep-err');
  err.style.display = 'none';
  const {type, ui} = _epState;
  const isKb = type === 'kb';
  const rawKeys = document.getElementById('ep-keys').value.trim();
  const rawCap  = document.getElementById('ep-cap').value.trim();
  const rawCmd  = document.getElementById('ep-cmd').value.trim();
  const rawArgs = document.getElementById('ep-args').value.trim();
  const rawCtx  = document.getElementById('ep-ctx').value.trim();
  if (isKb && !rawKeys) { err.textContent = 'Keys required.'; err.style.display = ''; return; }
  if (!isKb && !rawCap) { err.textContent = 'Caption required.'; err.style.display = ''; return; }
  if (!rawCmd)           { err.textContent = 'Command required.'; err.style.display = ''; return; }
  let args = null, context = null;
  if (rawArgs) { try { args = JSON.parse(rawArgs); } catch(e) { err.textContent = 'Args: ' + e.message; err.style.display = ''; return; } }
  if (isKb && rawCtx) { try { context = JSON.parse(rawCtx); } catch(e) { err.textContent = 'Context: ' + e.message; err.style.display = ''; return; } }
  let entry;
  if (isKb) {
    entry = {keys: rawKeys.split(',').map(s => s.trim()).filter(Boolean), command: rawCmd};
    if (args    != null) entry.args    = args;
    if (context != null) entry.context = context;
  } else {
    entry = {caption: rawCap, command: rawCmd};
    if (args != null) entry.args = args;
  }
  const isMenu = type === 'menu';
  const base = isKb ? '/kb' : isMenu ? '/menu' : '/cmd';
  const body = isMenu ? {menu: _currentMenu, index: ui, entry} : {index: ui, entry};
  api(base + '/edit', body).then(() => {
    setStatus('Saved', 'ok');
    closePanel();
    if (isKb) kbBuild(); else if (isMenu) menusBuild(); else cmdsBuild();
  }).catch(e => { err.textContent = 'Error: ' + e.message; err.style.display = ''; });
}

function panelOverride() {
  const b = D.kb.bindings[_epState.dataIdx];
  openAddModal('kb');
  document.getElementById('m-keys').value = (b.keys || []).join(',');
  document.getElementById('m-cmd').value  = b.command || '';
  if (b.args != null) document.getElementById('m-args').value = JSON.stringify(b.args, null, 2);
}

function panelBind() {
  const data = _epState.type === 'kb'
    ? D.kb.bindings[_epState.dataIdx]
    : D.cmds.bindings[_epState.dataIdx];
  openAddModal('kb');
  document.getElementById('m-cmd').value = data.command || '';
  if (data.args != null) document.getElementById('m-args').value = JSON.stringify(data.args, null, 2);
  setTimeout(() => { startRecord(); }, 120);
}

function openCmdPickerFor(target) {
  _pickerForPanel = (target === 'panel');
  openCmdPicker();
}

// ── Key Recorder ──────────────────────────────────────────────────────────────

let _recording = false;

function toggleRecord() { if (_recording) stopRecord(); else startRecord(); }

function startRecord() {
  _recording = true;
  const btn = document.getElementById('ep-rec');
  if (btn) { btn.textContent = '&#9632; Stop'; btn.classList.add('recording'); }
  const kf = document.getElementById('ep-keys');
  if (kf) { kf.placeholder = 'Press key combo…'; kf.style.background = '#fffbe6'; }
}

function stopRecord() {
  if (!_recording) return;
  _recording = false;
  const btn = document.getElementById('ep-rec');
  if (btn) { btn.innerHTML = '&#9210; Rec'; btn.classList.remove('recording'); }
  const kf = document.getElementById('ep-keys');
  if (kf) { kf.placeholder = 'e.g. ctrl+s'; kf.style.background = ''; }
}

function _mapKey(key) {
  const m = {'Enter':'enter','Escape':'escape','Backspace':'backspace','Delete':'delete',
    'Tab':'tab',' ':'space','ArrowUp':'up','ArrowDown':'down','ArrowLeft':'left','ArrowRight':'right',
    'Home':'home','End':'end','PageUp':'pageup','PageDown':'pagedown','Insert':'insert',
    'F1':'f1','F2':'f2','F3':'f3','F4':'f4','F5':'f5','F6':'f6',
    'F7':'f7','F8':'f8','F9':'f9','F10':'f10','F11':'f11','F12':'f12'};
  return m[key] || key.toLowerCase();
}

document.addEventListener('keydown', e => {
  if (!_recording) return;
  if (['Control','Shift','Alt','Meta'].includes(e.key)) return;
  if (e.key === 'Escape') { stopRecord(); e.preventDefault(); e.stopPropagation(); return; }
  e.preventDefault(); e.stopPropagation();
  const parts = [];
  if (e.ctrlKey)  parts.push('ctrl');
  if (e.altKey)   parts.push('alt');
  if (e.shiftKey) parts.push('shift');
  if (e.metaKey)  parts.push('super');
  parts.push(_mapKey(e.key));
  const field = document.getElementById('ep-keys');
  const cur = field.value.trim();
  field.value = cur ? cur + ',' + parts.join('+') : parts.join('+');
  stopRecord();
}, true);

// ── Keyboard shortcuts ────────────────────────────────────────────────────────

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (document.getElementById('picker-modal').classList.contains('open')) { closeCmdPicker(); return; }
    if (document.getElementById('ctx-modal').classList.contains('open'))    { closeCtxModal(); return; }
    if (document.getElementById('edit-modal').classList.contains('open'))   { closeModal(); return; }
    if (document.getElementById('ep').classList.contains('open'))           { closePanel(); return; }
  }
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    if (document.getElementById('edit-modal').classList.contains('open')) { saveModal(); return; }
    if (document.getElementById('ep').classList.contains('open') && _epState.ui >= 0) { panelApply(); return; }
  }
});

// ── Poll for external file changes ────────────────────────────────────────────

setInterval(() => {
  fetch('/ping').then(r => r.json()).then(d => {
    if (d.gen !== D.gen) location.reload();
  }).catch(() => {});
}, 20000);

// ── Init ──────────────────────────────────────────────────────────────────────

kbBuild();
cmdsBuild();
menusBuild();
</script>
</body>
</html>
"""


# ── HTTP server ────────────────────────────────────────────────────────────────

_data = {}
_gen = 0
_server = None
_port = None
_lock = threading.Lock()


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

    def _json_ok(self):
        with _lock:
            data = dict(_data)
        self._json({"ok": True, "data": data})

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
        try:
            self._dispatch(path, body)
        except Exception as e:
            self._json({"ok": False, "error": str(e)})

    def _dispatch(self, path, body):
        if path == "/kb/add":
            entries = _read_user_keymap()
            entries.append(body["entry"])
            _write_user_keymap(entries)
            _update_data()
            self._json_ok()

        elif path == "/kb/edit":
            idx, entry = body["index"], body["entry"]
            entries = _read_user_keymap()
            if not (isinstance(idx, int) and 0 <= idx < len(entries)):
                self._json({"ok": False, "error": f"index {idx} out of range"})
                return
            entries[idx] = entry
            _write_user_keymap(entries)
            _update_data()
            self._json_ok()

        elif path == "/kb/delete":
            idx = body["index"]
            entries = _read_user_keymap()
            if not (isinstance(idx, int) and 0 <= idx < len(entries)):
                self._json({"ok": False, "error": f"index {idx} out of range"})
                return
            entries.pop(idx)
            _write_user_keymap(entries)
            _update_data()
            self._json_ok()

        elif path == "/kb/move":
            idx = body["index"]
            direction = body["direction"]
            entries = _read_user_keymap()
            new_idx = idx + (1 if direction == "down" else -1)
            if not (isinstance(idx, int) and 0 <= new_idx < len(entries)):
                self._json({"ok": False, "error": "out of range"})
                return
            entries[idx], entries[new_idx] = entries[new_idx], entries[idx]
            _write_user_keymap(entries)
            _update_data()
            self._json_ok()

        elif path == "/cmd/add":
            entries = _read_user_commands()
            entries.append(body["entry"])
            _write_user_commands(entries)
            _update_data()
            self._json_ok()

        elif path == "/cmd/edit":
            idx, entry = body["index"], body["entry"]
            entries = _read_user_commands()
            if not (isinstance(idx, int) and 0 <= idx < len(entries)):
                self._json({"ok": False, "error": f"index {idx} out of range"})
                return
            entries[idx] = entry
            _write_user_commands(entries)
            _update_data()
            self._json_ok()

        elif path == "/cmd/delete":
            idx = body["index"]
            entries = _read_user_commands()
            if not (isinstance(idx, int) and 0 <= idx < len(entries)):
                self._json({"ok": False, "error": f"index {idx} out of range"})
                return
            entries.pop(idx)
            _write_user_commands(entries)
            _update_data()
            self._json_ok()

        elif path == "/cmd/move":
            idx = body["index"]
            direction = body["direction"]
            entries = _read_user_commands()
            new_idx = idx + (1 if direction == "down" else -1)
            if not (isinstance(idx, int) and 0 <= new_idx < len(entries)):
                self._json({"ok": False, "error": "out of range"})
                return
            entries[idx], entries[new_idx] = entries[new_idx], entries[idx]
            _write_user_commands(entries)
            _update_data()
            self._json_ok()

        elif path == "/menu/add":
            name, entry = body["menu"], body["entry"]
            menus = _read_user_menus()
            entries = menus.get(name, [])
            entries.append(entry)
            _write_user_menu(name, entries)
            _update_data()
            self._json_ok()

        elif path == "/menu/edit":
            name, idx, entry = body["menu"], body["index"], body["entry"]
            menus = _read_user_menus()
            entries = menus.get(name, [])
            if not (isinstance(idx, int) and 0 <= idx < len(entries)):
                self._json({"ok": False, "error": f"index {idx} out of range"})
                return
            entries[idx] = entry
            _write_user_menu(name, entries)
            _update_data()
            self._json_ok()

        elif path == "/menu/delete":
            name, idx = body["menu"], body["index"]
            menus = _read_user_menus()
            entries = menus.get(name, [])
            if not (isinstance(idx, int) and 0 <= idx < len(entries)):
                self._json({"ok": False, "error": f"index {idx} out of range"})
                return
            entries.pop(idx)
            _write_user_menu(name, entries)
            _update_data()
            self._json_ok()

        elif path == "/menu/move":
            name, idx, direction = body["menu"], body["index"], body["direction"]
            menus = _read_user_menus()
            entries = menus.get(name, [])
            new_idx = idx + (1 if direction == "down" else -1)
            if not (isinstance(idx, int) and 0 <= new_idx < len(entries)):
                self._json({"ok": False, "error": "out of range"})
                return
            entries[idx], entries[new_idx] = entries[new_idx], entries[idx]
            _write_user_menu(name, entries)
            _update_data()
            self._json_ok()

        else:
            self._json({"ok": False, "error": "unknown endpoint"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


class _Server(HTTPServer):
    allow_reuse_address = True


def _ensure_server():
    global _server, _port
    if _server is not None:
        return
    _server = _Server(("127.0.0.1", _FIXED_PORT), _Handler)
    _port = _FIXED_PORT
    threading.Thread(target=_server.serve_forever, daemon=True).start()


def _update_data():
    global _gen
    data = _build_data()
    with _lock:
        _gen += 1
        data["gen"] = _gen
        _data.clear()
        _data.update(data)


def _open():
    import webbrowser

    _ensure_server()
    _update_data()
    webbrowser.open(f"http://127.0.0.1:{_port}")
    sublime.status_message(f"ST Config: http://127.0.0.1:{_port}")


# ── ST lifecycle ──────────────────────────────────────────────────────────────


def plugin_unloaded():
    global _server
    if _server:
        _server.shutdown()
        _server.server_close()
        _server = None


# ── ST command ────────────────────────────────────────────────────────────────


class StConfigOpenCommand(sublime_plugin.WindowCommand):
    def run(self):
        threading.Thread(target=_open, daemon=True).start()
