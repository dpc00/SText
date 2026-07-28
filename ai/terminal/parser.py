"""Minimal ANSI/VT state machine (Claude/ratatui subset).

Pure Python — no Sublime imports. Safe to unit-test outside ST.
"""
from .colors import pack_attr, quantize256, BOLD, REVERSE, FAINT
from .screen import BLANK

_GROUND, _ESC, _CSI, _OSC = 0, 1, 2, 3


class Parser:
    def __init__(self, screen, force_main_screen=True):
        self.s = screen
        self.force_main_screen = force_main_screen
        self.state = _GROUND
        self.params = ""
        # Current SGR state. _fg/_bg are 1-based colour ids (0=default);
        # _flags holds BOLD/REVERSE (other styles are parsed but not rendered).
        self._fg = 0
        self._bg = 0
        self._flags = 0

    @property
    def _cur_attr(self):
        return pack_attr(self._fg, self._bg, self._flags)

    def feed(self, text):
        for ch in text:
            self._step(ch)

    def _step(self, ch):
        st = self.state
        o = ord(ch)
        if st == _GROUND:
            if ch == "\x1b":
                self.state = _ESC
            elif o == 0x0A or o == 0x0B or o == 0x0C:
                self.s.lf()
            elif ch == "\r":
                self.s.cr()
            elif ch == "\b":
                self.s.bs()
            elif ch == "\t":
                self.s.tab()
            elif o == 0x07:
                pass  # BEL
            elif o < 0x20 or o == 0x7F:
                pass  # other C0 / DEL -- ignore
            else:
                self.s.put_char(ch, self._cur_attr)
        elif st == _ESC:
            if ch == "[":
                self.state = _CSI
                self.params = ""
            elif ch == "]":
                self.state = _OSC
                self.params = ""
            elif ch == "7":
                self.s.save_cursor()
                self.state = _GROUND
            elif ch == "8":
                self.s.restore_cursor()
                self.state = _GROUND
            elif ch == "D":  # IND
                self.s.lf()
                self.state = _GROUND
            elif ch == "E":  # NEL
                self.s.cr()
                self.s.lf()
                self.state = _GROUND
            elif ch == "c":  # RIS
                self.s.reset()
                self._fg = self._bg = self._flags = 0
                self.state = _GROUND
            elif ch == "M":  # RI -- reverse index; rare, no-op for MVP
                self.state = _GROUND
            else:
                self.state = _GROUND  # ESC =, ESC >, ESC ( etc -- consume
        elif st == _CSI:
            if 0x30 <= o <= 0x3F:  # parameter bytes
                self.params += ch
            elif 0x20 <= o <= 0x2F:  # intermediates -- ignore
                pass
            elif 0x40 <= o <= 0x7E:  # final byte
                self._dispatch_csi(ch)
                self.state = _GROUND
            else:
                self.state = _GROUND
        elif st == _OSC:
            # terminate on BEL or ST (ESC \)
            if o == 0x07:
                self.state = _GROUND
            elif ch == "\\" and self.params.endswith("\x1b"):
                self.state = _GROUND
            else:
                self.params += ch

    def _ints(self, default=0):
        priv = self.params.startswith("?")
        raw = self.params.lstrip("?")
        parts = raw.split(";") if raw else []
        out = []
        for p in parts:
            out.append(int(p) if p.isdigit() else default)
        return priv, out

    def _parse_ext_color(self, p, j):
        """Parse a 38/48 extended colour spec starting at p[j] -> 1-based xterm id.

        ;5;N (256-colour) is taken directly (N is already a 256-palette index);
        ;2;r;g;b (truecolour) is quantized to the nearest xterm 256 entry.
        Returns 0 (default) on a malformed spec."""
        if j >= len(p):
            return 0
        if p[j] == 5 and j + 1 < len(p):
            n = p[j + 1]
            if 0 <= n <= 255:
                return n + 1
            return 0
        if p[j] == 2 and j + 3 < len(p):
            return quantize256(p[j + 1], p[j + 2], p[j + 3]) + 1
        return 0

    def _sgr(self, p):
        """Apply an SGR parameter list to the current fg/bg/flags.

        Only fg/bg/bold/reverse are rendered in v1; faint/italic/underline/
        strike are parsed (so the stream stays in sync) but do not affect the
        scope mapping."""
        if not p:
            p = [0]
        i = 0
        n = len(p)
        while i < n:
            c = p[i]
            if c == 0:
                self._fg = self._bg = self._flags = 0
            elif c == 1:
                self._flags |= BOLD
            elif c == 7:
                self._flags |= REVERSE
            elif c == 2:
                self._flags |= FAINT
            elif c == 22:
                # normal intensity: clears both bold and faint
                self._flags &= ~(BOLD | FAINT)
            elif c == 21:
                self._flags &= ~BOLD
            elif c == 27:
                self._flags &= ~REVERSE
            elif 3 <= c <= 6 or c == 8 or c == 9 or c in (23, 24, 28, 29):
                pass  # italic/underline/blink/conceal/strike + clears: parsed, not rendered
            elif 30 <= c <= 37:
                self._fg = c - 30 + 1
            elif c == 38 and i + 1 < n:
                self._fg = self._parse_ext_color(p, i + 1)
                if p[i + 1] == 5 and i + 2 < n:
                    i += 2
                elif p[i + 1] == 2 and i + 4 < n:
                    i += 4
            elif c == 39:
                self._fg = 0
            elif 40 <= c <= 47:
                self._bg = c - 40 + 1
            elif c == 48 and i + 1 < n:
                self._bg = self._parse_ext_color(p, i + 1)
                if p[i + 1] == 5 and i + 2 < n:
                    i += 2
                elif p[i + 1] == 2 and i + 4 < n:
                    i += 4
            elif c == 49:
                self._bg = 0
            elif 90 <= c <= 97:
                self._fg = c - 90 + 9
            elif 100 <= c <= 107:
                self._bg = c - 100 + 9
            i += 1

    def _dispatch_csi(self, final):
        priv, p = self._ints()
        s = self.s
        if final == "m":  # SGR -- select graphic rendition (colour/style)
            self._sgr(p)
            return
        if final in ("H", "f"):  # CUP / HVP
            r = (p[0] if len(p) > 0 and p[0] else 1) - 1
            c = (p[1] if len(p) > 1 and p[1] else 1) - 1
            s.move_abs(r, c)
        elif final == "A":
            s.move_rel(-(p[0] if p and p[0] else 1), 0)
        elif final == "B":
            s.move_rel(p[0] if p and p[0] else 1, 0)
        elif final == "C":
            s.move_rel(0, p[0] if p and p[0] else 1)
        elif final == "D":
            s.move_rel(0, -(p[0] if p and p[0] else 1))
        elif final == "J":
            s.erase_display(p[0] if p else 0)
        elif final == "K":
            s.erase_line(p[0] if p else 0)
        elif final == "X":  # ECH -- erase Ps chars from cursor (cursor does not move)
            # ConPTY leans on ECH heavily to blank cells mid-row when a TUI frame
            # shrinks a line; dropping it (the old "consumed-and-dropped" fallback)
            # left stale cells visible -- e.g. the /slash-menu mash where the
            # statusline and old menu items bled into the new filtered list.
            n = max(0, p[0] if p else 1)
            row = s.grid[s.y]
            arow = s.attrs[s.y]
            for c in range(s.x, min(s.x + n, s.cols)):
                row[c] = BLANK
                arow[c] = 0
            s.dirty = True
        elif final == "G":  # CHA -- cursor horizontal absolute
            s.move_abs(s.y, (p[0] if p and p[0] else 1) - 1)
        elif final == "d":  # VPA -- vertical position absolute
            s.move_abs((p[0] if p and p[0] else 1) - 1, s.x)
        elif final == "s":
            s.save_cursor()
        elif final == "u":
            s.restore_cursor()
        elif final in ("h", "l"):  # set / reset mode (private: 1049/2004/mouse/sync)
            if priv and "1049" in self.params:
                if not self.force_main_screen:
                    s.alt_screen = (final == "h")
            # all others consumed-and-dropped so the stream stays in sync
        elif final == "S":  # SU -- Scroll Up
            n = p[0] if p and p[0] else 1
            for _ in range(n):
                s._scroll_up()
        elif final == "T":  # SD -- Scroll Down
            n = p[0] if p and p[0] else 1
            for _ in range(n):
                s._scroll_down()
        # P, @, L, M, r, and any other finals: consumed-and-dropped.


