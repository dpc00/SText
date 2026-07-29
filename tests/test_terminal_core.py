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
    encode_click,
    encode_mouse,
    encode_wheel,
    pack_attr,
    paint_host_cursor,
    punch_host_cursor_region,
    quantize256,
    sanitize_pty_env,
    scope_name_for,
    st_button_to_proto,
    translate_key,
    view_point_to_cell,
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

    def test_truecolor_semicolon_rgb(self):
        s = Screen(10, 3)
        p = Parser(s)
        p.feed("\x1b[38;2;255;0;0mR")
        from ai.terminal.colors import scope_name_for
        scope = scope_name_for(s.attrs[0][0])
        self.assertIsNotNone(scope)
        # Quantized red should be a non-default fg on default bg.
        self.assertTrue(scope.startswith("ai.fb."))
        self.assertTrue(scope.endswith(".0"))

    def test_truecolor_colon_rgb(self):
        """Junie/Compose: 38:2:r:g:b without colour-space id."""
        s = Screen(10, 3)
        p = Parser(s)
        p.feed("\x1b[38:2:255:255:255mW")
        from ai.terminal.colors import scope_name_for, quantize256
        scope = scope_name_for(s.attrs[0][0])
        # white → palette index 15 or nearby grey/white (1-based in scope)
        fg = int(scope.split(".")[2])
        self.assertEqual(fg, quantize256(255, 255, 255) + 1)

    def test_truecolor_colon_with_colorspace(self):
        """ISO-8613-6 empty CS: 38:2::R:G:B must not eat R as green."""
        s = Screen(10, 3)
        p = Parser(s)
        p.feed("\x1b[38:2::255:128:64mX")
        from ai.terminal.colors import scope_name_for, quantize256
        scope = scope_name_for(s.attrs[0][0])
        fg = int(scope.split(".")[2])
        self.assertEqual(fg, quantize256(255, 128, 64) + 1)


class TestSchemeContrast(unittest.TestCase):
    def test_black_on_default_bg_becomes_readable(self):
        from ai.terminal.colors import scheme_colors_for, hex_luma
        fg, bg = scheme_colors_for(1, 0)  # ANSI black on default
        self.assertGreaterEqual(abs(hex_luma(fg) - hex_luma(bg)), 48)

    def test_default_fg_on_dark_bg_has_foreground(self):
        from ai.terminal.colors import scheme_colors_for, DEFAULT_FG_HEX
        fg, bg = scheme_colors_for(0, 2)  # default fg on red bg
        self.assertEqual(fg, DEFAULT_FG_HEX)
        self.assertTrue(fg.startswith("#"))
        self.assertTrue(bg.startswith("#"))

    def test_ensure_contrast_lifts_black_on_nearblack(self):
        from ai.terminal.colors import ensure_contrast
        self.assertEqual(ensure_contrast("#000000", "#000001"), "#FFFFFF")


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


class TestCaretContentEnd(unittest.TestCase):
    def _row(self, s: str, cols: int = 0):
        from ai.terminal.screen import Screen
        from ai.terminal.caret import content_end_col, input_start_col, find_prompt_row

        cols = cols or max(len(s), 40)
        scr = Screen(cols, 3)
        for i, ch in enumerate(s[:cols]):
            scr.grid[0][i] = ch
        return scr, content_end_col, input_start_col, find_prompt_row

    def test_excludes_right_clock_after_gap(self):
        """Grok: `> hello world` + pad + `7:52 AM` must end after 'world'."""
        #        012345678901234567890...
        line = "> hello world" + (" " * 20) + "7:52 AM"
        scr, content_end_col, input_start_col, _ = self._row(line, cols=len(line) + 2)
        self.assertEqual(input_start_col(scr, 0), 2)
        # after 'd' of world
        self.assertEqual(content_end_col(scr, 0), 2 + len("hello world"))

    def test_empty_field_ignores_clock(self):
        """Empty Grok prompt: only right clock → seat at input start."""
        line = "> " + (" " * 30) + "7:52 AM"
        scr, content_end_col, input_start_col, _ = self._row(line, cols=len(line) + 2)
        start = input_start_col(scr, 0)
        self.assertEqual(content_end_col(scr, 0), start)

    def test_single_spaces_inside_text_kept(self):
        line = "> a b c"
        scr, content_end_col, _, _ = self._row(line)
        self.assertEqual(content_end_col(scr, 0), len(line))

    def test_spaced_prompt_marker(self):
        """Junie pads before `>`."""
        line = "   > hi"
        scr, content_end_col, input_start_col, find_prompt_row = self._row(line)
        self.assertEqual(find_prompt_row(scr), 0)
        self.assertEqual(input_start_col(scr, 0), 5)
        self.assertEqual(content_end_col(scr, 0), 7)


class TestHostCursor(unittest.TestCase):
    def test_pads_when_absent_reports_host_painted(self):
        """Grok-style: pad past rstrip; host_painted so ST can draw phantom."""
        rows = [[(">", 0), (" ", 0), ("h", 0), ("i", 0)]]
        self.assertTrue(cell_needs_host_cursor(rows, 0, 4))
        out, painted = paint_host_cursor(rows, 0, 4)
        self.assertTrue(painted)
        self.assertEqual(len(out[0]), 5)
        ch, attr = out[0][4]
        self.assertEqual(ch, " ")
        self.assertEqual(attr, 0)
        off = cursor_text_offset(out, 0, 4)
        self.assertEqual(off, 4)

    def test_leaves_existing_reverse(self):
        """Claude-style: app already reverse-painted the cursor cell."""
        rows = [[("x", REVERSE)]]
        self.assertFalse(cell_needs_host_cursor(rows, 0, 0))
        out, painted = paint_host_cursor(rows, 0, 0)
        self.assertFalse(painted)
        self.assertEqual(out[0][0], ("x", REVERSE))

    def test_mid_line_keeps_char_no_attr_mutate(self):
        rows = [[("a", 0), ("b", 0), ("c", 0)]]
        out, painted = paint_host_cursor(rows, 0, 1)
        self.assertTrue(painted)
        self.assertEqual(out[0][1][0], "b")
        self.assertEqual(out[0][1][1], 0)
        self.assertEqual(out[0][0][1], 0)

    def test_punch_exclusive_over_color_run(self):
        """punch helper still isolates HOST_CURSOR_SCOPE if used."""
        rows = [[("a", pack_attr(fg=2)), ("b", pack_attr(fg=2)), ("c", pack_attr(fg=2))]]
        out, painted = paint_host_cursor(rows, 0, 1)
        self.assertTrue(painted)
        text, regs = build_text_and_regions(out)
        off = cursor_text_offset(out, 0, 1)
        self.assertEqual(off, 1)
        punched = punch_host_cursor_region(regs, off)
        host = [r for r in punched if r[2] == HOST_CURSOR_SCOPE]
        self.assertEqual(len(host), 1)
        self.assertEqual(host[0][:2], [1, 2])
        self.assertEqual(text[1], "b")


class TestMouseModes(unittest.TestCase):
    def test_decset_mouse_modes(self):
        s = Screen(40, 10)
        p = Parser(s)
        self.assertEqual(s.mouse_tracking, 0)
        self.assertFalse(s.mouse_sgr)
        p.feed("\x1b[?1000h\x1b[?1006h")
        self.assertEqual(s.mouse_tracking, 1000)
        self.assertTrue(s.mouse_sgr)
        p.feed("\x1b[?1002h")
        self.assertEqual(s.mouse_tracking, 1002)
        p.feed("\x1b[?1002l\x1b[?1000l")
        self.assertEqual(s.mouse_tracking, 0)
        self.assertTrue(s.mouse_sgr)  # 1006 still on
        p.feed("\x1b[?1006l")
        self.assertFalse(s.mouse_sgr)

    def test_ris_clears_modes(self):
        s = Screen(10, 5)
        p = Parser(s)
        p.feed("\x1b[?1000h\x1b[?1006h")
        p.feed("\x1bc")
        self.assertEqual(s.mouse_tracking, 0)
        self.assertFalse(s.mouse_sgr)

    def test_encode_sgr_click(self):
        seq = encode_click(0, 5, 12, sgr=True)
        self.assertEqual(seq, "\x1b[<0;5;12M\x1b[<0;5;12m")

    def test_encode_wheel(self):
        self.assertEqual(encode_wheel(True, 1, 1, sgr=True), "\x1b[<64;1;1M")
        self.assertEqual(encode_wheel(False, 2, 3, sgr=True), "\x1b[<65;2;3M")

    def test_encode_x10(self):
        seq = encode_mouse(0, 1, 1, press=True, sgr=False)
        self.assertEqual(seq, "\x1b[M" + chr(32) + chr(33) + chr(33))

    def test_view_point_to_cell(self):
        # history=2, click on view row 3 → screen row 1 (0-based) → cy=2
        self.assertEqual(
            view_point_to_cell(3, 4, hist_len=2, screen_rows=10, screen_cols=80),
            (5, 2),
        )
        # Click in scrollback
        self.assertIsNone(
            view_point_to_cell(0, 0, hist_len=2, screen_rows=10, screen_cols=80)
        )

    def test_st_button_map(self):
        self.assertEqual(st_button_to_proto(1), 0)
        self.assertEqual(st_button_to_proto(3), 2)
        self.assertIsNone(st_button_to_proto(9))


class TestSanitizePtyEnv(unittest.TestCase):
    def test_strips_no_color_and_fixes_dumb_term(self):
        out = sanitize_pty_env({
            "NO_COLOR": "1",
            "FORCE_COLOR": "0",
            "TERM": "dumb",
            "PATH": "C:\\bin",
        })
        self.assertNotIn("NO_COLOR", out)
        self.assertEqual(out["FORCE_COLOR"], "1")
        self.assertEqual(out["TERM"], "xterm-256color")
        self.assertEqual(out["COLORTERM"], "truecolor")
        self.assertEqual(out["PATH"], "C:\\bin")

    def test_profile_overrides_win(self):
        out = sanitize_pty_env(
            {"TERM": "dumb", "NO_COLOR": "1"},
            {"TERM": "xterm-kitty", "AI_TERMINAL_LOG_LINES": "1"},
        )
        self.assertEqual(out["TERM"], "xterm-kitty")
        self.assertEqual(out["AI_TERMINAL_LOG_LINES"], "1")
        self.assertNotIn("NO_COLOR", out)

    def test_keeps_good_term(self):
        out = sanitize_pty_env({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
        self.assertEqual(out["TERM"], "xterm-256color")
        self.assertEqual(out["COLORTERM"], "truecolor")


if __name__ == "__main__":
    unittest.main()
