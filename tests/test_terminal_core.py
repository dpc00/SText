"""Unit tests for pure terminal core (no Sublime required).

Run from repo root:
    python -m unittest tests.test_terminal_core -v
"""
from __future__ import annotations

import unittest

from ai.terminal import (
    HOST_CURSOR_SCOPE,
    Parser,
    Screen,
    build_text_and_regions,
    cell_needs_host_cursor,
    cursor_text_offset,
    pack_attr,
    paint_host_cursor,
    quantize256,
    scope_name_for,
    translate_key,
)
from ai.terminal.colors import FAINT, REVERSE


class TestKeys(unittest.TestCase):
    def test_enter_and_arrows(self):
        self.assertEqual(translate_key("enter"), "\r")
        self.assertEqual(translate_key("up"), "\x1b[A")
        self.assertEqual(translate_key("down"), "\x1b[B")

    def test_ctrl_c(self):
        self.assertEqual(translate_key("c", ctrl=True), "\x03")

    def test_printable(self):
        self.assertEqual(translate_key("a"), "a")


class TestColors(unittest.TestCase):
    def test_quantize_primary_red(self):
        self.assertEqual(quantize256(255, 0, 0), 1)

    def test_scope_default(self):
        self.assertIsNone(scope_name_for(0))

    def test_scope_red_fg(self):
        attr = pack_attr(fg=2)
        self.assertEqual(scope_name_for(attr), "ai.fb.2.0")

    def test_reverse_swaps(self):
        attr = pack_attr(fg=2, bg=5, flags=REVERSE)
        self.assertEqual(scope_name_for(attr), "ai.fb.5.2")

    def test_faint_dims(self):
        attr = pack_attr(fg=0, flags=FAINT)
        scope = scope_name_for(attr)
        self.assertIsNotNone(scope)
        self.assertTrue(scope.startswith("ai.fb."))

    def test_reverse_default_is_visible_scope(self):
        """TUI block cursor: reverse space on default colours must not be None."""
        from ai.terminal.colors import REVERSE as R
        attr = R  # reverse only, fg=0 bg=0
        scope = scope_name_for(attr)
        self.assertIsNotNone(scope)
        # black-on-white after resolving defaults then swapping
        self.assertEqual(scope, "ai.fb.1.16")

    def test_reverse_colored_swaps(self):
        from ai.terminal.colors import REVERSE as R
        attr = pack_attr(fg=2, bg=0) | R  # red on default bg -> black on red
        self.assertEqual(scope_name_for(attr), "ai.fb.1.2")


class TestScreen(unittest.TestCase):
    def test_put_and_cursor(self):
        s = Screen(10, 4)
        s.put_char("A")
        s.put_char("B")
        self.assertEqual(s.grid[0][0], "A")
        self.assertEqual(s.grid[0][1], "B")
        self.assertEqual((s.x, s.y), (2, 0))

    def test_scroll_into_history(self):
        s = Screen(5, 2, history_cap=10)
        s.put_char("1")
        s.cr()
        s.lf()
        s.put_char("2")
        s.cr()
        s.lf()
        self.assertEqual(len(s.history), 1)
        hist_chars = "".join(ch for ch, _ in s.history[0])
        self.assertIn("1", hist_chars)

    def test_resize_clips_history(self):
        s = Screen(10, 3, history_cap=5)
        s.history.append([("x", 0)] * 10)
        s.resize(4, 3)
        self.assertEqual(len(s.history[0]), 4)

    def test_alt_screen_hides_history(self):
        s = Screen(5, 2)
        s.history.append([("h", 0)])
        s.alt_screen = True
        rows, cy, cx = s.render_cells()
        self.assertEqual(len(rows), 2)
        self.assertEqual(cy, s.y)


class TestParser(unittest.TestCase):
    def test_plain_text(self):
        s = Screen(20, 5)
        p = Parser(s)
        p.feed("hi")
        self.assertEqual("".join(s.grid[0][:2]), "hi")

    def test_sgr_red(self):
        s = Screen(20, 5)
        p = Parser(s)
        p.feed("\x1b[31mR\x1b[0m")
        self.assertEqual(s.grid[0][0], "R")
        self.assertNotEqual(s.attrs[0][0], 0)

    def test_cup_and_erase(self):
        s = Screen(10, 5)
        p = Parser(s)
        p.feed("abcdef")
        p.feed("\x1b[1;1H")
        p.feed("\x1b[2K")
        self.assertEqual(s.grid[0][0], " ")
        self.assertEqual(s.x, 0)
        self.assertEqual(s.y, 0)

    def test_ech(self):
        s = Screen(10, 3)
        p = Parser(s)
        p.feed("ABCDE")
        p.feed("\x1b[1;2H")
        p.feed("\x1b[2X")
        self.assertEqual(s.grid[0][0], "A")
        self.assertEqual(s.grid[0][1], " ")
        self.assertEqual(s.grid[0][2], " ")
        self.assertEqual(s.grid[0][3], "D")

    def test_force_main_screen_ignores_alt(self):
        s = Screen(10, 3)
        p = Parser(s, force_main_screen=True)
        p.feed("\x1b[?1049h")
        self.assertFalse(s.alt_screen)
        p2 = Parser(Screen(10, 3), force_main_screen=False)
        p2.feed("\x1b[?1049h")
        self.assertTrue(p2.s.alt_screen)

    def test_reverse_sgr_sets_flag(self):
        s = Screen(10, 3)
        p = Parser(s)
        p.feed("\x1b[7mX\x1b[0m")
        self.assertEqual(s.grid[0][0], "X")
        self.assertTrue(s.attrs[0][0] & REVERSE)


class TestRender(unittest.TestCase):
    def test_coalesce_regions(self):
        s = Screen(10, 2)
        p = Parser(s)
        p.feed("\x1b[31mRR\x1b[0mxx")
        rows, _, _ = s.render_cells()
        text, regs = build_text_and_regions(rows)
        self.assertTrue(text.startswith("RRxx"))
        red_regs = [r for r in regs if r[2] == "ai.fb.2.0"]
        self.assertEqual(len(red_regs), 1)
        self.assertEqual(red_regs[0][1] - red_regs[0][0], 2)


class TestHostCursor(unittest.TestCase):
    def test_paints_reverse_when_absent(self):
        """Grok-style: cursor past rstripped blanks, no reverse cell."""
        rows = [[(">", 0), (" ", 0), ("h", 0), ("i", 0)]]
        self.assertTrue(cell_needs_host_cursor(rows, 0, 4))
        out, painted = paint_host_cursor(rows, 0, 4)  # insert point after "hi"
        self.assertTrue(painted)
        self.assertEqual(len(out[0]), 5)
        ch, attr = out[0][4]
        self.assertEqual(ch, " ")
        self.assertTrue(attr & REVERSE)
        text, regs = build_text_and_regions(out)
        self.assertTrue(any(r[2] == "ai.fb.1.16" for r in regs))
        off = cursor_text_offset(out, 0, 4)
        self.assertEqual(off, 4)
        self.assertEqual(text[off], " ")

    def test_leaves_existing_reverse(self):
        """Claude-style: app already reverse-painted the cursor cell."""
        rows = [[("x", REVERSE)]]
        self.assertFalse(cell_needs_host_cursor(rows, 0, 0))
        out, painted = paint_host_cursor(rows, 0, 0)
        self.assertFalse(painted)
        self.assertEqual(out[0][0], ("x", REVERSE))

    def test_mid_line_reverses_char(self):
        rows = [[("a", 0), ("b", 0), ("c", 0)]]
        out, painted = paint_host_cursor(rows, 0, 1)
        self.assertTrue(painted)
        self.assertEqual(out[0][1][0], "b")
        self.assertTrue(out[0][1][1] & REVERSE)
        self.assertEqual(out[0][0][1], 0)
        self.assertEqual(HOST_CURSOR_SCOPE, "ai.terminal.host_cursor")


if __name__ == "__main__":
    unittest.main()
