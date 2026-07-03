"""clear_buffer.py — hard-clear a Terminus terminal view.

Terminus's built-in 'cls'/'clear' only scrolls the visible area; history
and the internal pyte screen buffer remain intact, which wastes memory and
confuses MCP tools that read the full view content.  This command does a
true wipe:

  Phase 1 — send 'cls\\n' so the shell redraws its prompt at the bottom.
  Phase 2 — 300 ms later (enough for cls to complete), surgically rewrite
             the pyte screen buffer so only the prompt line survives, then
             force Terminus to re-render from the clean buffer.
"""

import sublime
import sublime_plugin


def _terminal_for(view):
    """Return the Terminus Terminal for view, or None if Terminus is absent.

    Imported lazily so a missing/removed Terminus package only breaks this
    one command, instead of failing the whole User package load (loader.py
    imports this module at top level).
    """
    try:
        from Terminus.terminus.terminal import Terminal
    except ImportError:
        return None
    return Terminal.from_id(view.id())


class ClearBufferCommand(sublime_plugin.TextCommand):
    """Send cls then scrub the Terminus screen buffer down to the prompt line.

    Two-phase approach:
      1. 'cls\\n' redraws the prompt so screen.cursor.y points at the prompt row.
      2. After a short delay, we promote that row to index 0, delete every
         other row, wipe scrollback history, reset the scroll offset, mark all
         rows dirty, then call terminus_nuke + terminus_render to rebuild the
         ST view from the now-clean buffer.

    Requires Terminus; silently does nothing on non-terminal views.
    """

    def run(self, edit):
        terminal = _terminal_for(self.view)
        if not terminal:
            return
        self.view.run_command("terminus_send_string", {"string": "cls\n"})
        # 300 ms lets the shell process cls and update screen.cursor.y before
        # we read it in _clean.
        sublime.set_timeout(lambda: self._clean(self.view), 300)

    def _clean(self, view):
        """Rewrite the pyte screen buffer so only the prompt line remains.

        pyte stores screen rows in screen.buffer, a dict keyed by row index.
        screen.cursor.y is the row where the shell drew its new prompt after cls.
        We copy that row to index 0 and delete everything else, giving Terminus
        a one-line buffer containing just the prompt.
        """
        terminal = _terminal_for(view)
        if not terminal:
            return
        screen = terminal.screen
        prompt_y = screen.cursor.y
        # Promote the prompt row to the top of the buffer.
        screen.buffer[0] = screen.buffer.get(prompt_y, screen.buffer.default_factory())
        for y in list(screen.buffer.keys()):
            if y != 0:
                del screen.buffer[y]
        screen.history.clear()  # wipe pyte's scrollback deque
        screen.cursor.y = 0  # move cursor to the surviving row
        terminal.offset = 0  # Terminus scroll offset: 0 = no scrollback visible
        screen.dirty.update(range(screen.lines))  # mark every row for redraw
        view.run_command("terminus_nuke")  # clear the ST text buffer
        view.run_command("terminus_render")  # repaint from the pyte screen
