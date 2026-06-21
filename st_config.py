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
        sublime.platform(), "Windows")

def _keymap_fnames():
    return {"Default.sublime-keymap", f"Default ({_plat_label()}).sublime-keymap"}

def _user_keymap_path():
    return os.path.join(sublime.packages_path(), "User",
                        f"Default ({_plat_label()}).sublime-keymap")

def _user_commands_path():
    return os.path.join(sublime.packages_path(), "User", "Default.sublime-commands")


# ── keybindings ───────────────────────────────────────────────────────────────

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
            cmd  = e.get("command", "")
            if not keys or not cmd:
                continue
            out.append({
                "keys":    keys,
                "key_str": ", ".join(keys) if isinstance(keys, list) else str(keys),
                "command": cmd,
                "args":    e.get("args"),
                "context": e.get("context"),
                "source":  pkg,
                "is_user": pkg == "User",
            })
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
        obj = {"keys": e["keys"], "command": e["command"]}
        if e.get("args")    is not None: obj["args"]    = e["args"]
        if e.get("context") is not None: obj["context"] = e["context"]
        lines.append(f"\t{json.dumps(obj, ensure_ascii=False)}{',' if i < len(entries)-1 else ''}")
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
            cmd     = e.get("command", "")
            if not caption and not cmd:
                continue
            uid = f"{path}::{caption}::{cmd}"
            if uid in seen:
                continue
            seen.add(uid)
            out.append({
                "caption": caption,
                "command": cmd,
                "args":    e.get("args"),
                "source":  pkg,
                "is_user": pkg == "User",
            })
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
        if e.get("caption"): obj["caption"] = e["caption"]
        obj["command"] = e["command"]
        if e.get("args") is not None: obj["args"] = e["args"]
        lines.append(f"\t{json.dumps(obj, ensure_ascii=False)}{',' if i < len(entries)-1 else ''}")
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
        caption  = item.get("caption", "")
        cmd      = item.get("command", "")
        children = item.get("children")
        if caption == "-":
            continue
        current = breadcrumb + ([caption] if caption else [])
        if caption or cmd:
            out.append({
                "path":    " › ".join(breadcrumb),
                "caption": caption,
                "command": cmd,
                "args":    item.get("args"),
                "source":  pkg,
            })
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


# ── combined data ─────────────────────────────────────────────────────────────

def _build_data():
    bindings = _load_all_bindings()
    key_count = defaultdict(int)
    for b in bindings:
        key_count[b["key_str"]] += 1
    for b in bindings:
        b["conflict"] = key_count[b["key_str"]] > 1
    return {
        "kb":      {"bindings": bindings,            "user_entries": _read_user_keymap()},
        "cmds":    {"bindings": _load_all_commands(), "user_entries": _read_user_commands()},
        "menus":   _load_all_menus(),
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

/* Header */
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
.hdr-btn.add{background:rgba(255,255,255,.3);border-color:rgba(255,255,255,.6);font-weight:600}

/* Tab bar */
.tab-bar{height:36px;background:var(--panel);border-bottom:2px solid var(--border);
         display:flex;align-items:stretch;padding:0 8px;flex-shrink:0}
.tab{padding:0 18px;cursor:pointer;font-size:13px;color:var(--muted);
     display:flex;align-items:center;gap:5px;
     border-bottom:2px solid transparent;margin-bottom:-2px;
     user-select:none;transition:color .1s}
.tab:hover{color:var(--text)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}

/* Body */
.body-area{flex:1;display:flex;flex-direction:column;overflow:hidden}
.tab-section{flex:1;display:flex;flex-direction:column;overflow:hidden}
.tab-section.hidden{display:none}

/* Filter bar */
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

/* Table */
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

/* Cell types */
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
.act-btn{background:none;border:none;cursor:pointer;padding:3px 7px;border-radius:3px;
         font-size:14px;color:var(--muted);transition:all .1s;line-height:1}
.act-btn:hover{background:#e8e8e8;color:var(--text)}
.act-btn.del:hover{background:#fde8e8;color:var(--warn)}
.dim{opacity:.4}
.no-results{padding:48px 16px;text-align:center;color:var(--muted);font-size:14px}

/* Footer */
footer{height:40px;background:var(--panel);border-top:1px solid var(--border);
       display:flex;align-items:center;padding:0 16px;gap:12px;flex-shrink:0}
#status-msg{font-size:12px;color:var(--muted)}
#status-msg.ok{color:#107c10}
#status-msg.err{color:var(--warn)}
#footer-count{font-size:12px;color:var(--muted)}

/* Modals */
.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);
                z-index:100;align-items:flex-start;justify-content:center;padding-top:60px}
.modal-backdrop.open{display:flex}
.modal-card{background:#fff;border-radius:6px;box-shadow:0 8px 32px rgba(0,0,0,.28);
            width:480px;max-height:80vh;display:flex;flex-direction:column;overflow:hidden}
.modal-hdr{padding:12px 16px;border-bottom:1px solid var(--border);
           font-weight:600;font-size:14px;display:flex;align-items:center}
.modal-hdr span{flex:1}
.modal-x{background:none;border:none;font-size:18px;cursor:pointer;
          color:var(--muted);line-height:1;padding:2px 6px}
.modal-x:hover{color:var(--text)}
.modal-body{padding:16px;overflow-y:auto;display:flex;flex-direction:column;gap:10px}
.field label{font-size:12px;font-weight:600;color:var(--muted);display:block;margin-bottom:3px}
.field .hint{font-weight:400;opacity:.75;font-size:11px}
.field input,.field textarea{width:100%;padding:7px 10px;border:1px solid #b3b3b3;
                              border-radius:4px;font-size:13px;font-family:inherit;
                              outline:none;resize:vertical}
.field input:focus,.field textarea:focus{
  border-color:var(--accent);box-shadow:0 0 0 2px rgba(0,120,212,.2)}
.ferr{color:var(--warn);font-size:12px;margin-top:3px;display:none}
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
  <button id="btn-add" class="hdr-btn add" onclick="openAddModal()">&#43; Add</button>
  <button class="hdr-btn" onclick="closeApp()">&#10005;&#160;Close</button>
</header>

<div class="tab-bar">
  <div class="tab active" data-tab="keys" onclick="setTab('keys')">&#9000; Keys</div>
  <div class="tab"        data-tab="cmds" onclick="setTab('cmds')">&#8984; Commands</div>
  <div class="tab"        data-tab="menus" onclick="setTab('menus')">&#9776; Menus</div>
</div>

<div class="body-area">

  <!-- Keys -->
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
          <th style="width:72px"></th>
        </tr></thead>
        <tbody id="tb-keys"></tbody>
      </table>
      <div class="no-results" id="nr-keys" style="display:none">No matching bindings.</div>
    </main>
  </div>

  <!-- Commands -->
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
          <th style="width:72px"></th>
        </tr></thead>
        <tbody id="tb-cmds"></tbody>
      </table>
      <div class="no-results" id="nr-cmds" style="display:none">No matching commands.</div>
    </main>
  </div>

  <!-- Menus -->
  <div class="tab-section hidden" id="tab-menus">
    <div class="filter-bar" id="menu-chips"></div>
    <main>
      <table>
        <thead><tr>
          <th style="width:130px">Menu</th>
          <th style="width:190px">Path</th>
          <th style="width:190px">Caption</th>
          <th style="width:180px">Command</th>
          <th style="width:120px">Source</th>
        </tr></thead>
        <tbody id="tb-menus"></tbody>
      </table>
      <div class="no-results" id="nr-menus" style="display:none">No matching items.</div>
    </main>
  </div>

</div>

<footer>
  <span id="status-msg">Ready</span>
  <div class="spacer"></div>
  <span id="footer-count"></span>
</footer>

<!-- Add/Edit modal (shared) -->
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
        <input type="text" id="m-cmd" autocomplete="off" spellcheck="false">
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
  return esc(s.length > 36 ? s.slice(0, 34) + '…' : s);
}

function setStatus(msg, cls) {
  const el = document.getElementById('status-msg');
  el.textContent = msg; el.className = cls || '';
}

function setFooter(t) { document.getElementById('footer-count').textContent = t; }
function closeApp()   { fetch('/close').finally(() => window.close()); }

// ── Tab switching ─────────────────────────────────────────────────────────────

function setTab(name) {
  _tab = name;
  document.querySelectorAll('.tab').forEach(el =>
    el.classList.toggle('active', el.dataset.tab === name));
  document.querySelectorAll('.tab-section').forEach(el =>
    el.classList.toggle('hidden', el.id !== 'tab-' + name));
  document.getElementById('btn-add').style.display = name === 'menus' ? 'none' : '';
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
  const tbody = document.getElementById('tb-keys');
  tbody.innerHTML = '';
  let ui = -1;
  D.kb.bindings.forEach((b, i) => {
    if (b.is_user) ui++;
    const tr = document.createElement('tr');
    tr.dataset.ks = b.key_str.toLowerCase();
    tr.dataset.cmd = b.command.toLowerCase();
    tr.dataset.user = b.is_user ? '1' : '0';
    tr.dataset.conflict = b.conflict ? '1' : '0';
    if (b.is_user)   { tr.classList.add('user-row'); tr.dataset.ui = ui; }
    if (b.conflict)  tr.classList.add('conflict');
    const ctxCell = b.context != null
      ? `<button class="ctx-btn" onclick="showCtx(${i})">ctx</button>`
      : '<span style="color:#ddd">—</span>';
    const act = b.is_user
      ? `<button class="act-btn" title="Edit" onclick="openEditModal('kb',${ui})">&#9998;</button>` +
        `<button class="act-btn del" title="Delete" onclick="doDelete('kb',${ui})">&#128465;</button>`
      : '';
    tr.innerHTML =
      `<td><span class="key-seq">${renderKeys(b.keys)}</span></td>` +
      `<td class="mono">${esc(b.command)}</td>` +
      `<td class="muted-sm trunc" title="${esc(JSON.stringify(b.args??''))}">${shortJson(b.args)}</td>` +
      `<td style="text-align:center">${ctxCell}</td>` +
      `<td class="src${b.is_user?' is-user':''}">${esc(b.source)}</td>` +
      `<td style="text-align:right;padding-right:10px">${act}</td>`;
    tbody.appendChild(tr);
  });
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
    const ok = (_kbFilter==='all' || (_kbFilter==='user' && tr.dataset.user==='1') ||
                (_kbFilter==='conflicts' && tr.dataset.conflict==='1')) &&
               (!_search || tr.dataset.ks.includes(_search) || tr.dataset.cmd.includes(_search));
    tr.classList.toggle('hidden', !ok);
    if (ok) vis++;
  });
  document.getElementById('nr-keys').style.display = vis ? 'none' : '';
  setFooter(vis < rows.length ? `${vis} of ${rows.length} bindings` : `${rows.length} bindings`);
}

// ── Commands ──────────────────────────────────────────────────────────────────

function cmdsBuild() {
  const tbody = document.getElementById('tb-cmds');
  tbody.innerHTML = '';
  let ui = -1;
  D.cmds.bindings.forEach((c) => {
    if (c.is_user) ui++;
    const tr = document.createElement('tr');
    tr.dataset.cap  = (c.caption||'').toLowerCase();
    tr.dataset.cmd  = (c.command||'').toLowerCase();
    tr.dataset.user = c.is_user ? '1' : '0';
    if (c.is_user) { tr.classList.add('user-row'); tr.dataset.ui = ui; }
    const act = c.is_user
      ? `<button class="act-btn" title="Edit" onclick="openEditModal('cmd',${ui})">&#9998;</button>` +
        `<button class="act-btn del" title="Delete" onclick="doDelete('cmd',${ui})">&#128465;</button>`
      : '';
    tr.innerHTML =
      `<td>${esc(c.caption)}</td>` +
      `<td class="mono">${esc(c.command)}</td>` +
      `<td class="muted-sm trunc" title="${esc(JSON.stringify(c.args??''))}">${shortJson(c.args)}</td>` +
      `<td class="src${c.is_user?' is-user':''}">${esc(c.source)}</td>` +
      `<td style="text-align:right;padding-right:10px">${act}</td>`;
    tbody.appendChild(tr);
  });
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
    const ok = (_cmdFilter==='all' || (_cmdFilter==='user' && tr.dataset.user==='1')) &&
               (!_search || tr.dataset.cap.includes(_search) || tr.dataset.cmd.includes(_search));
    tr.classList.toggle('hidden', !ok);
    if (ok) vis++;
  });
  document.getElementById('nr-cmds').style.display = vis ? 'none' : '';
  setFooter(vis < rows.length ? `${vis} of ${rows.length} commands` : `${rows.length} commands`);
}

// ── Menus ─────────────────────────────────────────────────────────────────────

function menusBuild() {
  const bar = document.getElementById('menu-chips');
  bar.innerHTML = '';
  const keys = ['all', ...Object.keys(D.menus)];
  const total = Object.values(D.menus).reduce((s, a) => s + a.length, 0);
  keys.forEach(k => {
    const el = document.createElement('div');
    el.className = 'chip' + (k === 'all' ? ' active' : '');
    el.dataset.mf = k;
    el.onclick = () => menusSetFilter(k);
    const cnt = k === 'all' ? total : (D.menus[k]||[]).length;
    el.innerHTML = `${esc(k === 'all' ? 'All' : k)} <span class="cnt">${cnt}</span>`;
    bar.appendChild(el);
  });

  const tbody = document.getElementById('tb-menus');
  tbody.innerHTML = '';
  Object.entries(D.menus).forEach(([menuName, items]) => {
    items.forEach(item => {
      const tr = document.createElement('tr');
      tr.dataset.menu = menuName.toLowerCase();
      tr.dataset.cap  = (item.caption||'').toLowerCase();
      tr.dataset.cmd  = (item.command||'').toLowerCase();
      tr.dataset.path = (item.path||'').toLowerCase();
      if (!item.command) tr.classList.add('dim');
      tr.innerHTML =
        `<td class="muted-sm">${esc(menuName)}</td>` +
        `<td class="muted-sm trunc" title="${esc(item.path)}">${esc(item.path)}</td>` +
        `<td>${esc(item.caption)}</td>` +
        `<td class="mono">${esc(item.command)}</td>` +
        `<td class="src">${esc(item.source)}</td>`;
      tbody.appendChild(tr);
    });
  });
  menusApply();
}

function menusSetFilter(f) {
  _menuFilter = f;
  document.querySelectorAll('[data-mf]').forEach(el =>
    el.classList.toggle('active', el.dataset.mf === f));
  menusApply();
}

function menusApply() {
  const rows = document.querySelectorAll('#tb-menus tr');
  let vis = 0;
  rows.forEach(tr => {
    const ok = (_menuFilter==='all' || tr.dataset.menu===_menuFilter.toLowerCase()) &&
               (!_search || tr.dataset.cap.includes(_search) || tr.dataset.cmd.includes(_search) ||
                tr.dataset.path.includes(_search));
    tr.classList.toggle('hidden', !ok);
    if (ok) vis++;
  });
  document.getElementById('nr-menus').style.display = vis ? 'none' : '';
  setFooter(vis < rows.length ? `${vis} of ${rows.length} items` : `${rows.length} items`);
}

// ── Context viewer ────────────────────────────────────────────────────────────

function showCtx(i) {
  document.getElementById('ctx-content').textContent =
    JSON.stringify(D.kb.bindings[i].context, null, 2);
  document.getElementById('ctx-modal').classList.add('open');
}
function closeCtxModal() { document.getElementById('ctx-modal').classList.remove('open'); }

// ── Add / Edit modal ──────────────────────────────────────────────────────────

function openAddModal() {
  _editIdx  = null;
  _editType = _tab === 'cmds' ? 'cmd' : 'kb';
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
  document.getElementById('m-args').value    = e.args    != null ? JSON.stringify(e.args,    null, 2) : '';
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

  const base = isKb ? '/kb' : '/cmd';
  const [url, body] = _editIdx === null
    ? [base+'/add',  {entry}]
    : [base+'/edit', {index: _editIdx, entry}];

  fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
    .then(r=>r.json()).then(r=>{
      if (r.ok) { closeModal(); setStatus('Saved ✓','ok'); setTimeout(()=>location.reload(),400); }
      else setStatus('Error: '+(r.error||'unknown'),'err');
    }).catch(()=>setStatus('Network error','err'));
}

function doDelete(type, ui) {
  const label = type==='kb' ? 'keymap' : 'commands';
  if (!confirm(`Remove from User ${label}?`)) return;
  fetch('/'+type+'/delete', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({index: ui})
  }).then(r=>r.json()).then(r=>{
    if (r.ok) { setStatus('Deleted ✓','ok'); setTimeout(()=>location.reload(),400); }
    else setStatus('Error: '+(r.error||'unknown'),'err');
  }).catch(()=>setStatus('Network error','err'));
}

// ── Poll ──────────────────────────────────────────────────────────────────────
(function(){
  const gen = D.gen;
  setInterval(()=>{
    fetch('/ping').then(r=>r.json()).then(d=>{ if(d.gen!==gen) location.reload(); }).catch(()=>{});
  }, 2000);
})();

kbBuild();
cmdsBuild();
menusBuild();
</script>
</body>
</html>
"""


# ── HTTP server ────────────────────────────────────────────────────────────────

_data   = {}
_gen    = 0
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
            self._json({"ok": True})

        elif path == "/kb/edit":
            idx, entry = body["index"], body["entry"]
            entries = _read_user_keymap()
            if not (isinstance(idx, int) and 0 <= idx < len(entries)):
                self._json({"ok": False, "error": f"index {idx} out of range"}); return
            entries[idx] = entry
            _write_user_keymap(entries)
            _update_data()
            self._json({"ok": True})

        elif path == "/kb/delete":
            idx = body["index"]
            entries = _read_user_keymap()
            if not (isinstance(idx, int) and 0 <= idx < len(entries)):
                self._json({"ok": False, "error": f"index {idx} out of range"}); return
            entries.pop(idx)
            _write_user_keymap(entries)
            _update_data()
            self._json({"ok": True})

        elif path == "/cmd/add":
            entries = _read_user_commands()
            entries.append(body["entry"])
            _write_user_commands(entries)
            _update_data()
            self._json({"ok": True})

        elif path == "/cmd/edit":
            idx, entry = body["index"], body["entry"]
            entries = _read_user_commands()
            if not (isinstance(idx, int) and 0 <= idx < len(entries)):
                self._json({"ok": False, "error": f"index {idx} out of range"}); return
            entries[idx] = entry
            _write_user_commands(entries)
            _update_data()
            self._json({"ok": True})

        elif path == "/cmd/delete":
            idx = body["index"]
            entries = _read_user_commands()
            if not (isinstance(idx, int) and 0 <= idx < len(entries)):
                self._json({"ok": False, "error": f"index {idx} out of range"}); return
            entries.pop(idx)
            _write_user_commands(entries)
            _update_data()
            self._json({"ok": True})

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
