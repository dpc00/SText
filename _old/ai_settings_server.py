"""ai_settings_server.py — Award-winning ST Settings browser.

Launched as a subprocess by ai_settings.py. Uses only Python stdlib.
Communicates changes back to ST via a callback HTTP server in the plugin.

Usage:
    python ai_settings_server.py --data FILE --callback URL --port PORT
"""

import argparse
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen



# ── HTML / CSS / JS ──────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title id="page-title">Sublime Text Settings</title>
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
const D = __DATA__;   // injected by server

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
  const sections = D.show_order;  // [[section, [keys]], ...]
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
      const tr = buildRow(key);
      tbody.appendChild(tr);
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
        ${hasUsefulDefault ? `<button class="reset-lnk" title="Restore default value" onclick="resetKey('${key}')">↩ default</button>` : ''}
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
  // update sidebar badges
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
  setStatus('Closing…', '');
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

// Poll for server restart and auto-reload this tab
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
// Scroll to key if we navigated here from a search result
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
_callback_url = ""
_data_file_path = ""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress request logs

    def _send(self, body, status=200, ct="text/html; charset=utf-8"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj), status, "application/json")

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        return json.loads(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            page = HTML.replace("__DATA__", json.dumps(_data))
            self._send(page)
        elif path == "/ping":
            try:
                with open(_data_file_path, encoding="utf-8") as f:
                    fresh = json.load(f)
                if fresh.get("gen", 0) != _data.get("gen", 0):
                    _data.clear()
                    _data.update(fresh)
            except Exception:
                pass
            self._json({"gen": _data.get("gen", 0)})
        elif path == "/close":
            self._send("Closing...")
            threading.Thread(target=_shutdown, daemon=True).start()
        else:
            self._send("Not found", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except Exception:
            self._json({"ok": False, "error": "bad JSON"}, 400)
            return

        if path in ("/apply", "/reset", "/navigate"):
            try:
                req = Request(
                    _callback_url + path,
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(req, timeout=5) as resp:
                    result = json.loads(resp.read())
                self._json(result)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        else:
            self._json({"ok": False, "error": "unknown endpoint"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def _shutdown():
    import time
    time.sleep(0.3)
    os._exit(0)


def main():
    global _data, _callback_url, _data_file_path

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-file", required=True)
    parser.add_argument("--callback", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--gen", type=int, default=0)
    args = parser.parse_args()

    _data_file_path = args.data_file
    with open(args.data_file, encoding="utf-8") as f:
        _data = json.load(f)
    _data["gen"] = args.gen
    _callback_url = args.callback.rstrip("/")

    font_db = Path.home() / ".claude" / "font_db.json"
    if font_db.exists():
        try:
            db = json.loads(font_db.read_text(encoding="utf-8"))
            _data["fonts"] = db.get("all", [])
            _data["mono_fonts"] = db.get("mono", [])
        except Exception:
            pass
    server = HTTPServer(("127.0.0.1", args.port), _Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
