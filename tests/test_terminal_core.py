"""Unit tests for pure terminal core (no Sublime required).

Run from repo root:
    python -m unittest tests.test_terminal_core -v
"""
from __future__ import annotations

import unittest

from ai.terminal import (
    Parser,
    Screen,
    adjust_display_caret,
    build_text_and_regions,
    pack_attr,
    quantize256,
    scope_name_for,
    translate_key,
)
from ai.terminal.caret import (
    content_end_col,
    input_start_col,
    nudge_input_caret,
    pad_row_for_caret,
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


class TestDisplayCaret(unittest.TestCase):
    def _claude_like_screen(self):
        """Prompt `>\\xa0hi` + border + status footer (Claude-shaped)."""
        s = Screen(40, 12)
        s.grid[4][0] = ">"
        s.grid[4][1] = "\u00a0"
        s.grid[4][2] = "h"
        s.grid[4][3] = "i"
        for c in range(s.cols):
            s.grid[5][c] = "\u2500"
        for i, ch in enumerate("  Weekly: 22%"):
            s.grid[8][i] = ch
        return s

    def test_input_start_is_two_after_prompt_marker(self):
        s = self._claude_like_screen()
        self.assertEqual(input_start_col(s, 4), 2)
        self.assertEqual(content_end_col(s, 4), 4)  # after 'hi'

    def test_pin_when_parked_on_status(self):
        s = self._claude_like_screen()
        s.x, s.y = 4, 4  # on prompt at end of 'hi'
        rows, cy, cx = s.render_cells()
        adjust_display_caret(s, cy, cx)
        self.assertEqual(s.input_caret_x, 4)
        s.x, s.y = 20, 8  # park on status
        rows, cy, cx = s.render_cells()
        cy2, cx2 = adjust_display_caret(s, cy, cx)
        self.assertEqual(cy2, 4)
        self.assertEqual(cx2, 4)

    def test_nudge_left_when_parked(self):
        s = self._claude_like_screen()
        s.x, s.y = 4, 4
        adjust_display_caret(s, *s.render_cells()[1:])
        s.x, s.y = 20, 8
        self.assertTrue(nudge_input_caret(s, -1))
        self.assertEqual(s.input_caret_x, 3)  # on 'i'
        # cannot enter the `>\\xa0` prefix
        s.input_caret_x = 2
        nudge_input_caret(s, -1)
        self.assertEqual(s.input_caret_x, 2)

    def test_mid_prompt_trusts_hardware(self):
        s = self._claude_like_screen()
        s.x, s.y = 2, 4  # on 'h'
        rows, cy, cx = s.render_cells()
        cy2, cx2 = adjust_display_caret(s, cy, cx)
        self.assertEqual(cx2, 2)

    def test_pad_row(self):
        s = self._claude_like_screen()
        s.x, s.y = 20, 8
        rows, cy, cx = s.render_cells()
        cy2, cx2 = adjust_display_caret(s, cy, cx)
        padded = pad_row_for_caret(rows, cy2, 4)
        self.assertGreaterEqual(len(padded[cy2]), 4)

    def test_no_prompt_no_adjust(self):
        s = Screen(20, 5)
        s.x, s.y = 3, 2
        rows, cy, cx = s.render_cells()
        self.assertEqual(adjust_display_caret(s, cy, cx), (cy, cx))


if __name__ == "__main__":
    unittest.main()
