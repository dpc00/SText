"""Cursor-aware terminal screen grid + scrollback.

Pure Python — no Sublime imports. Safe to unit-test outside ST.
"""
import collections

from .colors import rstrip_cells

BLANK = " "


class Screen:
    def __init__(self, cols, rows, history_cap=300):
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self.x = 0
        self.y = 0
        self.grid = [[BLANK] * self.cols for _ in range(self.rows)]
        # Per-cell packed colour attr, parallel to grid. 0 = default (no region).
        self.attrs = [[0] * self.cols for _ in range(self.rows)]
        # Scrollback: rows that scroll off the top are captured here.
        # Cap is a user setting (scrollback_history_size); does NOT auto-size
        # on window resize. Stored as rstripped [(ch, attr), ...] cell-lists.
        self.history = collections.deque(maxlen=history_cap)
        self.saved = (0, 0)
        self.alt_screen = False
        self.dirty = True
        # Last hardware cursor column while on the `>` prompt row. Used by
        # adjust_display_caret when Claude parks the cursor on the status bar
        # so ST restores the exact input column (not end-of-text - 1).
        self.input_caret_x = None

    def resize(self, cols, rows):
        cols, rows = max(1, cols), max(1, rows)
        new = [[BLANK] * cols for _ in range(rows)]
        new_attrs = [[0] * cols for _ in range(rows)]
        for r in range(min(rows, self.rows)):
            srow = self.grid[r]
            arow = self.attrs[r]
            for c in range(min(cols, self.cols)):
                new[r][c] = srow[c]
                new_attrs[r][c] = arow[c]
        self.grid = new
        self.attrs = new_attrs
        # Clip scrollback rows to the new width when the screen shrinks.
        if cols < self.cols:
            new_hist = collections.deque(maxlen=self.history.maxlen)
            for row_cells in self.history:
                new_hist.append(rstrip_cells(row_cells[:cols]))
            self.history = new_hist
        self.cols, self.rows = cols, rows
        self.x = min(self.x, cols - 1)
        self.y = min(self.y, rows - 1)
        self.dirty = True

    def reset(self):
        self.grid = [[BLANK] * self.cols for _ in range(self.rows)]
        self.attrs = [[0] * self.cols for _ in range(self.rows)]
        self.history.clear()
        self.x = self.y = 0
        self.dirty = True

    def set_history_cap(self, cap):
        """Swap the scrollback deque to a new maxlen, preserving contents."""
        cap = max(0, int(cap))
        if cap == self.history.maxlen:
            return
        self.history = collections.deque(self.history, maxlen=cap)

    def _scroll_up(self):
        popped = [(self.grid[0][c], self.attrs[0][c]) for c in range(self.cols)]
        self.history.append(rstrip_cells(popped))
        self.grid.pop(0)
        self.attrs.pop(0)
        self.grid.append([BLANK] * self.cols)
        self.attrs.append([0] * self.cols)

    def _scroll_down(self):
        self.grid.pop()
        self.attrs.pop()
        self.grid.insert(0, [BLANK] * self.cols)
        self.attrs.insert(0, [0] * self.cols)

    def put_char(self, ch, attr=0):
        if self.x >= self.cols:
            self.x = 0
            self._line_feed()
        self.grid[self.y][self.x] = ch
        self.attrs[self.y][self.x] = attr
        self.x += 1
        self.dirty = True

    def _line_feed(self):
        self.y += 1
        if self.y >= self.rows:
            self._scroll_up()
            self.y = self.rows - 1

    def lf(self):
        self._line_feed()
        self.dirty = True

    def cr(self):
        self.x = 0
        self.dirty = True

    def bs(self):
        if self.x > 0:
            self.x -= 1
        self.dirty = True

    def tab(self):
        self.x = min(((self.x // 8) + 1) * 8, self.cols - 1)
        self.dirty = True

    def move_abs(self, r, c):
        self.y = max(0, min(r, self.rows - 1))
        self.x = max(0, min(c, self.cols - 1))
        self.dirty = True

    def move_rel(self, dy, dx):
        self.y = max(0, min(self.y + dy, self.rows - 1))
        self.x = max(0, min(self.x + dx, self.cols - 1))
        self.dirty = True

    def erase_display(self, n):
        if n == 2 or n == 3:
            self.grid = [[BLANK] * self.cols for _ in range(self.rows)]
            self.attrs = [[0] * self.cols for _ in range(self.rows)]
            if n == 3:
                # CSI 3J = erase scrollback (and screen); 2J leaves scrollback.
                self.history.clear()
        elif n == 0:
            for c in range(self.x, self.cols):
                self.grid[self.y][c] = BLANK
                self.attrs[self.y][c] = 0
            for r in range(self.y + 1, self.rows):
                self.grid[r] = [BLANK] * self.cols
                self.attrs[r] = [0] * self.cols
        elif n == 1:
            for r in range(0, self.y):
                self.grid[r] = [BLANK] * self.cols
                self.attrs[r] = [0] * self.cols
            for c in range(0, self.x + 1):
                self.grid[self.y][c] = BLANK
                self.attrs[self.y][c] = 0
        self.dirty = True

    def erase_line(self, n):
        row = self.grid[self.y]
        arow = self.attrs[self.y]
        if n == 0:
            for c in range(self.x, self.cols):
                row[c] = BLANK
                arow[c] = 0
        elif n == 1:
            for c in range(0, self.x + 1):
                row[c] = BLANK
                arow[c] = 0
        elif n == 2:
            for c in range(self.cols):
                row[c] = BLANK
                arow[c] = 0
        self.dirty = True

    def save_cursor(self):
        self.saved = (self.x, self.y)
        self.dirty = True

    def restore_cursor(self):
        self.x, self.y = self.saved
        self.x = min(self.x, self.cols - 1)
        self.y = min(self.y, self.rows - 1)
        self.dirty = True

    def render_cells(self):
        """Return (rows, cy, cx) for rendering.

        rows is a list of [(ch, attr), ...] cell-lists. History is prepended
        only when not on the alt screen (fullscreen TUI mode).
        """
        grid_rows = []
        cy_in_grid = self.y
        cx = self.x
        for i in range(self.rows):
            srow = self.grid[i]
            arow = self.attrs[i]
            if i == self.y:
                x = max(self.x, 0)
                body = [(srow[c], arow[c]) for c in range(min(x, self.cols))]
                tail = [(srow[c], arow[c]) for c in range(x, self.cols)]
                grid_rows.append(body + rstrip_cells(tail))
            else:
                cells = [(srow[c], arow[c]) for c in range(self.cols)]
                grid_rows.append(rstrip_cells(cells))
        if self.alt_screen:
            return grid_rows, cy_in_grid, cx
        hist = list(self.history)
        return hist + grid_rows, len(hist) + cy_in_grid, cx
