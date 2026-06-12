"""claude_tab_manager.py — continuously log the Claude Terminus tab to disk.

Purpose
-------
The Terminus view named "Claude" is the live Claude Code session.  This plugin
does two things:

  1. Incremental logging (automatic, background):
     Every _CHECK_MS milliseconds, _tick() compares the view's current size to
     the last-logged size.  If new characters have appeared, it appends only the
     delta to today's log file (~/.claude/conversation_logs/claude_YYYY-MM-DD.log).
     This captures the full session transcript without duplicating text.

  2. Manual buffer trim (CtmTrimNowCommand):
     Terminus caps the *visible* scrollback via scrollback_history_size, but the
     ST text buffer keeps growing.  When the buffer exceeds that limit, this
     command removes lines from the top in batches, logging each batch before
     deletion so nothing is lost.  Batch size is max(excess, ceil(limit/10)) to
     avoid O(n²) single-line-at-a-time deletion on very large buffers.
"""

import datetime
import math
import os
from pathlib import Path

import sublime  # type: ignore
import sublime_plugin  # type: ignore

_LOG_DIR = str(Path.home() / ".claude" / "conversation_logs")
_CHECK_MS = 4000  # how often to poll the view for new content (ms)

# Maps view ID → character offset of the last text we logged.
# Survives plugin reloads because it lives at module scope.
_last_size = {}  # view_id -> last logged character offset


def _claude_view():
    """Return the first view named 'Claude' across all windows, or None."""
    for w in sublime.windows():
        for v in w.views():
            if v.name() == "Claude":
                return v
    return None


def _today_log() -> str:
    """Return the path to today's log file, creating the directory if needed."""
    os.makedirs(_LOG_DIR, exist_ok=True)
    return os.path.join(_LOG_DIR, f"claude_{datetime.date.today().isoformat()}.log")


def _append_log(text: str) -> None:
    """Append *text* to today's log file; silently ignore I/O errors."""
    try:
        with open(_today_log(), "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def _tick():
    """Poll the Claude view for new content and schedule the next tick.

    Uses character-offset bookmarking: we remember how many characters we've
    logged (stored in _last_size by view ID).  Each tick we read only the new
    slice [prev_size, current_size], append it to the log, then update the
    bookmark.  This is O(new_chars) rather than O(total_chars).

    The next tick is always scheduled at the end, even when no Claude view is
    open, so the loop resumes automatically when one is created later.
    """
    v = _claude_view()
    if v:
        vid = v.id()
        size = v.size()
        prev = _last_size.get(vid, 0)
        if size > prev:
            new_text = v.substr(sublime.Region(prev, size))
            _append_log(new_text)
            _last_size[vid] = size
    sublime.set_timeout(_tick, _CHECK_MS)


class CtmTrimNowCommand(sublime_plugin.TextCommand):
    """Remove lines from the top of the Claude view to stay within the Terminus scrollback limit.

    Algorithm:
      - Read scrollback_history_size from Terminus settings (default 500).
      - While the view has more lines than that limit:
          * Compute how many lines to delete: whichever is larger —
            the raw excess (lastrow+1 - n) or ceil(n/10).
            The floor of n/10 prevents removing just one line at a time,
            which would be O(n²) for large overflows.
          * Log the lines being deleted (so they aren't lost from history).
          * Erase the top region from the ST text buffer.
          * Adjust terminal.offset (Terminus's scroll position) so the visible
            content doesn't jump.
      - Prints the final line count to the ST console.

    Run this from the command palette or bind it to a key for manual cleanup.
    Terminus's own trimming is non-destructive to the ST buffer; this actually
    shrinks it.
    """

    def run(self, edit):
        try:
            from Terminus.terminus.terminal import Terminal  # type: ignore
        except ImportError:
            return
        v = self.view
        terminal = Terminal.from_id(v.id())
        if not terminal:
            return
        n = sublime.load_settings("Terminus.sublime-settings").get(
            "scrollback_history_size", 500
        )
        lastrow = v.rowcol(v.size())[0]
        while lastrow + 1 > n:
            # Delete at least 10 % of the limit per iteration to avoid O(n²).
            m = max(lastrow + 1 - n, math.ceil(n / 10))
            top_region = sublime.Region(0, v.line(v.text_point(m - 1, 0)).end() + 1)
            _append_log(v.substr(top_region))  # preserve deleted lines in log
            v.erase(edit, top_region)
            # Keep Terminus's scroll offset consistent after we shrank the buffer.
            terminal.offset = max(0, terminal.offset - m)
            lastrow = v.rowcol(v.size())[0]
        print(f"claude_tab_manager: trimmed to {lastrow + 1} lines")


def plugin_loaded():
    """Start the polling loop when Sublime loads this plugin."""
    sublime.set_timeout(_tick, _CHECK_MS)
