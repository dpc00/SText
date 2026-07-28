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
from ai.terminal.caret import pad_row_for_caret
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
        # fg index 1 (red, 1-based pack) => ai.fb.1.0? pack_attr(fg=1) is red ANSI
        attr = pack_attr(fg=2)  # 1-based: 2 => xterm index 1 = red
        self.assertEqual(scope_name_for(attr), "ai.fb.2.0")

    def test_reverse_swaps(self):
        attr = pack_attr(fg=2, bg=5, flags=REVERSE)
        self.assertEqual(scope_name_for(attr), "ai.fb.5.2")

    def test_faint_dims(self):
        attr = pack_attr(fg=0, flags=FAINT)  # default fg dimmed
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
        s.lf()  # should scroll first line into history
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
        p.feed("\x1b[1;1H")  # home
        p.feed("\x1b[2K")  # erase line
        self.assertEqual(s.grid[0][0], " ")
        self.assertEqual(s.x, 0)
        self.assertEqual(s.y, 0)

    def test_ech(self):
        s = Screen(10, 3)
        p = Parser(s)
        p.feed("ABCDE")
        p.feed("\x1b[1;2H")  # row1 col2
        p.feed("\x1b[2X")  # erase 2 chars
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
        # one region for the two red cells
        red_regs = [r for r in regs if r[2] == "ai.fb.2.0"]
        self.assertEqual(len(red_regs), 1)
        self.assertEqual(red_regs[0][1] - red_regs[0][0], 2)


class TestDisplayCaret(unittest.TestCase):
    def _claude_like_screen(self):
        """Minimal Claude-style frame: prompt row + border + status footer."""
        s = Screen(40, 12)
        # row 4: prompt
        s.grid[4][0] = ">"
        s.grid[4][1] = "\u00a0"
        for i, ch in enumerate("hi"):
            s.grid[4][2 + i] = ch
        # row 5: border
        for c in range(s.cols):
            s.grid[5][c] = "\u2500"
        # row 8: status (where Claude parks the cursor)
        for i, ch in enumerate("  Weekly: 22%"):
            s.grid[8][i] = ch
        return s

    def test_pin_to_prompt_when_cursor_on_status(self):
        s = self._claude_like_screen()
        # '>\xa0hi' content_end = 4. Hardware often sits at 4 (blank under cursor).
        s.x, s.y = 4, 4
        rows, cy, cx = s.render_cells()
        cy2, cx2 = adjust_display_caret(s, cy, cx)
        self.assertEqual(s.input_caret_x, 4)
        self.assertEqual(cx2, 4)
        # Then Claude parks on status — restore remembered column.
        s.x, s.y = 20, 8
        rows, cy, cx = s.render_cells()
        cy2, cx2 = adjust_display_caret(s, cy, cx)
        self.assertEqual(cy2, 4)
        self.assertEqual(cx2, 4)

    def test_clamp_hardware_x_past_content(self):
        """Hardware x past content clamps to content end."""
        s = self._claude_like_screen()
        # content_end for '>\xa0hi' is 4; claim hardware at 5
        s.x, s.y = 5, 4
        rows, cy, cx = s.render_cells()
        cy2, cx2 = adjust_display_caret(s, cy, cx)
        self.assertEqual(cx2, 4)
        self.assertEqual(s.input_caret_x, 4)

    def test_cursor_on_last_glyph_seats_after_it(self):
        """Hardware on final input char → ST caret after that char (not on it)."""
        s = self._claude_like_screen()
        # 'i' is last glyph at col 3; content_end 4
        s.x, s.y = 3, 4
        rows, cy, cx = s.render_cells()
        cy2, cx2 = adjust_display_caret(s, cy, cx)
        self.assertEqual(cx2, 4)

    def test_pin_fallback_without_memory(self):
        s = self._claude_like_screen()
        s.input_caret_x = None
        s.x, s.y = 20, 8
        rows, cy, cx = s.render_cells()
        cy2, cx2 = adjust_display_caret(s, cy, cx)
        self.assertEqual(cy2, 4)
        # '>\xa0hi' last non-blank at col 3 -> end 4
        self.assertEqual(cx2, 4)

    def test_reject_wild_eol_cup_on_prompt(self):
        s = self._claude_like_screen()
        s.x, s.y = 39, 4  # CUP to EOL while "erasing" — not real input col
        rows, cy, cx = s.render_cells()
        adjust_display_caret(s, cy, cx)
        # Must not remember 39
        self.assertNotEqual(s.input_caret_x, 39)
        s.x, s.y = 20, 8
        rows, cy, cx = s.render_cells()
        cy2, cx2 = adjust_display_caret(s, cy, cx)
        self.assertEqual(cy2, 4)
        self.assertEqual(cx2, 4)  # content end for '>\xa0hi'

    def test_pad_row_for_caret_extends_rstripped_prompt(self):
        s = self._claude_like_screen()
        s.x, s.y = 20, 8  # not on prompt -> prompt row fully rstripped
        rows, cy, cx = s.render_cells()
        cy2, cx2 = adjust_display_caret(s, cy, cx)
        # Without pad, prompt row is short; with pad, length >= caret col.
        padded = pad_row_for_caret(rows, cy2, 5)
        self.assertGreaterEqual(len(padded[cy2]), 5)

    def test_trust_cursor_mid_prompt(self):
        s = self._claude_like_screen()
        # 'h' at col 2 — mid content (last glyph is 'i' at 3)
        s.x, s.y = 2, 4
        rows, cy, cx = s.render_cells()
        cy2, cx2 = adjust_display_caret(s, cy, cx)
        self.assertEqual(cy2, cy)
        self.assertEqual(cx2, 2)
        self.assertEqual(s.input_caret_x, 2)

    def test_no_prompt_no_adjust(self):
        s = Screen(20, 5)
        s.x, s.y = 3, 2
        rows, cy, cx = s.render_cells()
        self.assertEqual(adjust_display_caret(s, cy, cx), (cy, cx))

if __name__ == "__main__":
    unittest.main()
