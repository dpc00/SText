#!/usr/bin/env python3
"""mock_agent_cli.py -- deterministic fake agent for ai_terminal resize testing.

Usage (manually, outside ST):
    python tests/mock_agent_cli.py [--cols N] [--rows N]

Usage inside SText:
    Set ai_terminal profile launch_command to:
        ["python", "C:\\Users\\donal\\projects\\SText\\tests\\mock_agent_cli.py"]

What it does:
    - Forces primary-screen output (no alt-screen DECSET 1049).
    - Echoes a status banner with the current terminal size and a frame counter.
    - Maintains a "conversation" of long, wrapping paragraphs that scroll up.
    - On SIGWINCH (Windows: window resize signal from ConPTY), re-emits the
      current visible frame so you can observe replay / oscillation bugs.
    - Reads single-key commands from stdin:
        s / enter  -> spew another long paragraph
        r          -> redraw current frame (simulate TUI resize response)
        q / ^C     -> exit cleanly
    - Colors output with SGR so the ai_terminal ANSI parser is exercised.
    - Writes a local log so you can correlate PTY events with the .cast file.

The goal is a zero-token, 100%% reproducible workload for the resize bug.
"""
import argparse
import os
import signal
import sys
import time

# Make sure stdout is a real terminal-like stream (PTY).
out = sys.stdout.buffer


def _log(msg):
    try:
        log_path = os.path.expanduser("~/data/logs/ai_terminal/mock_agent_cli.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def get_size():
    """Best-effort terminal size from os.get_terminal_size; defaults if none."""
    try:
        ts = os.get_terminal_size(sys.stdout.fileno())
        return ts.columns, ts.lines
    except Exception:
        return 80, 24


colors = ["31", "32", "33", "34", "35", "36"]


class MockAgent:
    def __init__(self, initial_cols=80, initial_rows=24):
        self.cols, self.rows = initial_cols, initial_rows
        self.frame = 0
        self.turn = 0
        self.lines = []  # current visible screen content (list of str)
        self._resize_count = 0
        self._last_cols, self._last_rows = 0, 0
        self._running = True

    def _write(self, data):
        out.write(data.encode("utf-8", "replace"))
        out.flush()

    def _clear(self):
        # Use primary-screen clear: ESC[2J then home, no alt-screen switching.
        self._write("\x1b[2J\x1b[H")

    def _word_wrap(self, text, width=None):
        width = width or self.cols
        words = text.split()
        lines = []
        cur = ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > width:
                lines.append(cur)
                cur = w
            else:
                cur = (cur + " " + w) if cur else w
        if cur:
            lines.append(cur)
        return lines

    def _make_paragraph(self, n):
        # Long paragraph designed to wrap multiple times and push scrollback.
        filler = (
            "mock agent response line"
            if n % 2 == 0
            else "user prompt replay stress test"
        )
        body = " ".join([f"{filler} #{n*1000+i}" for i in range(40)])
        return body

    def _banner(self):
        color = colors[self.frame % len(colors)]
        banner = (
            f"\x1b[1;{color}m[MockAgent]\x1b[0m "
            f"cols={self.cols} rows={self.rows} "
            f"frame={self.frame} resizes={self._resize_count} "
            f"turn={self.turn} | press s= spew, r=redraw, q=quit"
        )
        return banner[: self.cols]

    def _redraw(self, reason=""):
        self.frame += 1
        self._clear()
        # Top banner
        self._write(self._banner() + "\r\n")
        # Re-emit stored visible lines (this is what a TUI does on resize).
        visible = self.lines[-(self.rows - 2) :]
        for line in visible:
            self._write(line + "\r\n")
        # Prompt line at bottom
        self._write("\x1b[1;32m>\x1b[0m ")
        _log(f"redraw reason={reason} cols={self.cols} rows={self.rows} frame={self.frame}")

    def _spew(self):
        self.turn += 1
        para = self._make_paragraph(self.turn)
        # Wrap at current cols so the PTY does the wrapping, not ST soft-wrap.
        wrapped = self._word_wrap(para, self.cols)
        # Prepend a colored speaker header.
        header = f"\x1b[1;{colors[self.turn % len(colors)]}mTurn {self.turn}:\x1b[0m"
        wrapped[0] = header + " " + wrapped[0]
        self.lines.extend(wrapped)
        # Keep only enough to fill a few screens; old lines roll into scrollback.
        self.lines = self.lines[-(self.rows * 5) :]
        self._redraw(reason="spew")

    def _on_resize(self, signum=None, frame=None):
        old = (self._last_cols, self._last_rows)
        self.cols, self.rows = get_size()
        self._last_cols, self._last_rows = self.cols, self.rows
        if (self.cols, self.rows) != old:
            self._resize_count += 1
            self._redraw(reason="sigwinch")

    def run(self):
        # Initial measure.
        self.cols, self.rows = get_size()
        self._last_cols, self._last_rows = self.cols, self.rows
        _log(f"start cols={self.cols} rows={self.rows}")

        # Hide cursor like a real TUI; use primary screen only.
        self._write("\x1b[?25l")
        self._clear()
        self._redraw(reason="startup")

        # Seed some content so there is scrollback immediately.
        for _ in range(3):
            self._spew()

        if hasattr(signal, "SIGWINCH"):
            signal.signal(signal.SIGWINCH, self._on_resize)

        # Non-blocking stdin read loop.
        while self._running:
            try:
                ch = sys.stdin.read(1)
            except Exception:
                ch = ""
            if not ch:
                time.sleep(0.05)
                continue
            if ch in ("q", "Q", "\x03"):  # q or Ctrl+C
                self._running = False
                break
            if ch in ("s", "S", "\r", "\n"):
                self._spew()
            elif ch in ("r", "R"):
                self._on_resize(reason="manual_r")
            else:
                # Echo unknown keys as a tiny status update to keep stream alive.
                self._write(f"\x1b[{self.rows};1H\x1b[K key={repr(ch)} ")
                out.flush()

        # Restore cursor and exit.
        self._write("\x1b[?25h\r\n[mock_agent exited]\r\n")
        _log("exit")


def main():
    parser = argparse.ArgumentParser(description="Mock agent CLI for ai_terminal testing")
    parser.add_argument("--cols", type=int, default=0, help="Initial cols hint")
    parser.add_argument("--rows", type=int, default=0, help="Initial rows hint")
    args = parser.parse_args()

    cols, rows = get_size()
    if args.cols:
        cols = args.cols
    if args.rows:
        rows = args.rows

    agent = MockAgent(initial_cols=cols, initial_rows=rows)
    agent.run()


if __name__ == "__main__":
    main()
