"""panic_dialog.py — Quote-and-Reply dialog for Claude conversations.

Opens an HTML sheet with Claude's last response. User selects text to quote
it into a reply textarea, then clicks Send to inject back into Terminus.

Commands:
  panic_open   — open the dialog (reads last response from JSONL transcript)
"""

import glob
import json
import os
import urllib.parse

import sublime
import sublime_plugin

_AI_VIEW_SETTING = "ai_logger"
_SHEET_NAME = "Panic — Quote & Reply"


# ── JSONL reader ──────────────────────────────────────────────────────────────

def _last_claude_response():
    """Return last assistant text from the most recent JSONL transcript."""
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
    for view in window.views():
        if view.settings().get(_AI_VIEW_SETTING):
            view.run_command("terminus_send_string", {"string": text + "\n"})
            window.focus_view(view)
            return
    sublime.error_message("No Ai terminal found — open Claude Code first.")


# ── HTML builder ──────────────────────────────────────────────────────────────

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 13px;
  background: #1e1e2e;
  color: #cdd6f4;
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}

/* ── response pane ── */
#response {
  flex: 1 1 55%;
  overflow-y: auto;
  padding: 16px;
  border-bottom: 2px solid #313244;
  line-height: 1.75;
  user-select: text;
  cursor: text;
}
.para {
  margin-bottom: 14px;
  padding: 4px 8px;
  border-radius: 4px;
  white-space: pre-wrap;
  word-break: break-word;
}
.para:hover { background: #28283e; }

/* ── reply pane ── */
#reply-section {
  flex: 0 0 auto;
  padding: 10px 16px 0;
}
#reply-label {
  font-size: 11px;
  color: #6c7086;
  text-transform: uppercase;
  letter-spacing: .06em;
  margin-bottom: 5px;
}
#reply {
  width: 100%;
  height: 160px;
  background: #24273a;
  color: #cdd6f4;
  border: 1px solid #45475a;
  border-radius: 6px;
  padding: 10px;
  font: inherit;
  resize: vertical;
}
#reply:focus { border-color: #89b4fa; outline: none; }

/* ── buttons ── */
#buttons {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  padding: 8px 16px 12px;
  flex-shrink: 0;
}
button {
  padding: 7px 22px;
  border: none;
  border-radius: 5px;
  font: inherit;
  cursor: pointer;
}
#send-btn { background: #89b4fa; color: #1e1e2e; font-weight: bold; }
#send-btn:hover { background: #b4befe; }
#cancel-btn { background: #313244; color: #cdd6f4; }
#cancel-btn:hover { background: #45475a; }

/* ── quote popup ── */
#quote-popup {
  position: fixed;
  display: none;
  background: #89b4fa;
  color: #1e1e2e;
  padding: 5px 13px;
  border-radius: 5px;
  font-size: 12px;
  font-weight: bold;
  cursor: pointer;
  box-shadow: 0 3px 10px rgba(0,0,0,.5);
  z-index: 1000;
  white-space: nowrap;
  pointer-events: auto;
}
#quote-popup:hover { background: #b4befe; }
</style>
</head>
<body>

<div id="quote-popup" onclick="insertQuote()">Quote ↓</div>
<div id="response"></div>

<div id="reply-section">
  <div id="reply-label">Your reply</div>
  <textarea id="reply" placeholder="Select text above to quote it, then type your comment below the quote…"></textarea>
</div>

<div id="buttons">
  <button id="cancel-btn" onclick="location.href='cancel:'">Cancel</button>
  <button id="send-btn" onclick="doSend()">Send ↵</button>
</div>

<script>
var RESPONSE_TEXT = RESPONSE_JSON_PLACEHOLDER;

// Render paragraphs
var container = document.getElementById('response');
var paras = RESPONSE_TEXT.split(/\n{2,}/);
paras.forEach(function(p) {
  p = p.trim();
  if (!p) return;
  var div = document.createElement('div');
  div.className = 'para';
  div.textContent = p;
  container.appendChild(div);
});

// ── Quote popup ──────────────────────────────────────────────────────────────
var _sel = '';

document.addEventListener('mouseup', function(e) {
  if (e.target.id === 'quote-popup') return;
  var sel = window.getSelection().toString().trim();
  if (sel.length > 5) {
    _sel = sel;
    var popup = document.getElementById('quote-popup');
    popup.style.display = 'block';
    var x = Math.min(e.pageX - 10, window.innerWidth - 140);
    var y = e.pageY - 44;
    if (y < 4) y = e.pageY + 10;
    popup.style.left = x + 'px';
    popup.style.top = y + 'px';
  } else {
    hidePopup();
  }
});

document.addEventListener('mousedown', function(e) {
  if (e.target.id !== 'quote-popup') hidePopup();
});

function hidePopup() {
  document.getElementById('quote-popup').style.display = 'none';
}

function insertQuote() {
  var ta = document.getElementById('reply');
  var quoted = _sel.split('\n').map(function(l){ return '> ' + l; }).join('\n') + '\n\n';
  var pos = ta.selectionEnd;
  var before = ta.value.substring(0, pos);
  if (before.length && !before.endsWith('\n\n')) before += '\n\n';
  var after = ta.value.substring(pos);
  ta.value = before + quoted + after;
  var newPos = (before + quoted).length;
  ta.focus();
  ta.setSelectionRange(newPos, newPos);
  hidePopup();
  // scroll reply into view
  ta.scrollTop = ta.scrollHeight;
}

// ── Send ─────────────────────────────────────────────────────────────────────
function doSend() {
  var reply = document.getElementById('reply').value.trim();
  if (!reply) return;
  location.href = 'send:' + encodeURIComponent(reply);
}

// ctrl+enter sends
document.addEventListener('keydown', function(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') doSend();
});
</script>

</body>
</html>
"""


def _build_html(response_text):
    placeholder = json.dumps(response_text)
    return _HTML_TEMPLATE.replace("RESPONSE_JSON_PLACEHOLDER", placeholder)


# ── Command ───────────────────────────────────────────────────────────────────

class PanicOpenCommand(sublime_plugin.WindowCommand):
    """Open the Quote-and-Reply dialog with Claude's last response."""

    _sheet = None

    def run(self, response_text=None):
        if response_text is None:
            response_text = _last_claude_response()
        if not response_text:
            sublime.error_message("No Claude response found in transcript.")
            return
        html = _build_html(response_text)
        PanicOpenCommand._sheet = self.window.new_html_sheet(
            _SHEET_NAME,
            html,
            on_navigate=self._on_navigate,
        )

    def _on_navigate(self, href):
        if href == "cancel:":
            self._close_sheet()
        elif href.startswith("send:"):
            reply = urllib.parse.unquote(href[5:])
            _send_to_terminus(self.window, reply)
            self._close_sheet()

    def _close_sheet(self):
        sheet = PanicOpenCommand._sheet
        if sheet:
            try:
                sheet.close()
            except Exception:
                pass
            PanicOpenCommand._sheet = None
