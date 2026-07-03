"""ai_terminal.py -- bare-bones owned terminal for the Claude CLI.

Replaces the Terminus dependency for AI launch. No third-party packages: pure
ctypes against the Windows ConPTY (Pseudoconsole) API, plus a small cursor-aware
ANSI renderer tailored to the subset Claude's ratatui TUI emits. Because all of
the rendering/state code is ours, every bug is fixable here.

Architecture (one file, mirroring ai_sdk.py):
  _Pty     -- ConPTY wrapper (ctypes). Spawns the child, gives us a byte stream.
  _Screen  -- single-buffer cursor-aware grid (cols x rows) of chars.
  _Parser  -- minimal ANSI state machine feeding _Screen.
  _Terminal-- owns a _Pty + _Screen + _Parser; registry keyed by view id.
  renderer -- debounced, walks _Screen -> view text on the main thread.
  listener -- forwards keystrokes from the view to the PTY; kills PTY on close.

Commands (ST names):
  ai_terminal_open_here / ai_terminal_open_in_editor
  ai_terminal_send_string / ai_terminal_keypress / ai_terminal_render
  ai_terminal_nuke / ai_terminal_noop / ai_terminal_dump_screen

Note on input: ST does not fire on_text_command for unbound printable keys, so
Default.sublime-keymap binds every printable/special key to ai_terminal_keypress
(gated by setting.ai_terminal_view); ai_terminal_keypress translates the key to
terminal bytes and writes them to the PTY. The on_text_command listener is kept
as a fallback for any key-bound commands that still dispatch as insert/move.
"""

import codecs
import collections
import os
import threading

import sublime
import sublime_plugin

# ─── ctypes ConPTY binding (guarded: a failure must not crash loader.py) ─────

_PTY_OK = False
_k32 = None

if os.name == "nt":
    try:
        import ctypes
        from ctypes import (
            Structure,
            POINTER,
            byref,
            c_void_p,
            c_char,
            c_ulong,
            sizeof,
            windll,
        )
        from ctypes.wintypes import HANDLE, DWORD, WORD, BOOL, LPCWSTR, LPBYTE, SHORT

        # wintypes does not export HRESULT; it is a signed LONG.
        HRESULT = ctypes.c_long

        _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
        _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
        _CREATE_UNICODE_ENVIRONMENT = 0x00000400
        _STARTF_USESTDHANDLES = 0x00000100

        class _COORD(Structure):
            _fields_ = [("X", SHORT), ("Y", SHORT)]

        class _SECURITY_ATTRIBUTES(Structure):
            _fields_ = [("nLength", DWORD),
                        ("lpSecurityDescriptor", c_void_p),
                        ("bInheritHandle", BOOL)]

        class _STARTUPINFOW(Structure):
            _fields_ = [("cb", DWORD), ("lpReserved", c_void_p),
                        ("lpDesktop", c_void_p), ("lpTitle", c_void_p),
                        ("dwX", DWORD), ("dwY", DWORD),
                        ("dwXSize", DWORD), ("dwYSize", DWORD),
                        ("dwXCountChars", DWORD), ("dwYCountChars", DWORD),
                        ("dwFillAttribute", DWORD), ("dwFlags", DWORD),
                        ("wShowWindow", WORD), ("cbReserved2", WORD),
                        ("lpReserved2", LPBYTE),
                        ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE)]

        class _STARTUPINFOEXW(Structure):
            _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", c_void_p)]

        class _PROCESS_INFORMATION(Structure):
            _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE),
                        ("dwProcessId", DWORD), ("dwThreadId", DWORD)]

        _k32 = windll.kernel32
        # Set argtypes/restype on EVERY function -- without these ctypes truncates
        # 64-bit HANDLEs to c_int and ConPTY silently corrupts.
        _k32.CreatePipe.argtypes = [POINTER(HANDLE), POINTER(HANDLE),
                                    POINTER(_SECURITY_ATTRIBUTES), DWORD]
        _k32.CreatePipe.restype = BOOL
        _k32.CreatePseudoConsole.argtypes = [_COORD, HANDLE, HANDLE, DWORD, POINTER(HANDLE)]
        _k32.CreatePseudoConsole.restype = HRESULT
        _k32.ResizePseudoConsole.argtypes = [HANDLE, _COORD]
        _k32.ResizePseudoConsole.restype = HRESULT
        _k32.ClosePseudoConsole.argtypes = [HANDLE]
        _k32.ClosePseudoConsole.restype = None
        _k32.InitializeProcThreadAttributeList.argtypes = [c_void_p, DWORD, DWORD, POINTER(c_ulong)]
        _k32.InitializeProcThreadAttributeList.restype = BOOL
        _k32.UpdateProcThreadAttribute.argtypes = [c_void_p, DWORD, DWORD,
                                                   c_void_p, c_ulong,
                                                   c_void_p, POINTER(c_ulong)]
        _k32.UpdateProcThreadAttribute.restype = BOOL
        _k32.DeleteProcThreadAttributeList.argtypes = [c_void_p]
        _k32.DeleteProcThreadAttributeList.restype = None
        _k32.CreateProcessW.argtypes = [LPCWSTR, ctypes.c_wchar_p, c_void_p, c_void_p, BOOL,
                                        DWORD, c_void_p, LPCWSTR,
                                        POINTER(_STARTUPINFOEXW), POINTER(_PROCESS_INFORMATION)]
        _k32.CreateProcessW.restype = BOOL
        # Buffer arg must match the read buffer type. The reader uses a
        # (c_char * N) array, so the param is POINTER(c_char) -- LPBYTE
        # (POINTER(c_ubyte)) raises "expected LP_c_byte instance instead of
        # c_char_Array_N" on the first ReadFile and kills the reader thread.
        _k32.ReadFile.argtypes = [HANDLE, POINTER(c_char), DWORD, POINTER(DWORD), c_void_p]
        _k32.ReadFile.restype = BOOL
        # write() passes a `bytes` object; c_char_p accepts bytes directly.
        _k32.WriteFile.argtypes = [HANDLE, ctypes.c_char_p, DWORD, POINTER(DWORD), c_void_p]
        _k32.WriteFile.restype = BOOL
        _k32.GetExitCodeProcess.argtypes = [HANDLE, POINTER(DWORD)]
        _k32.GetExitCodeProcess.restype = BOOL
        _k32.TerminateProcess.argtypes = [HANDLE, DWORD]
        _k32.TerminateProcess.restype = BOOL
        _k32.CloseHandle.argtypes = [HANDLE]
        _k32.CloseHandle.restype = BOOL
        _k32.GetProcessHeap.restype = ctypes.c_void_p
        _k32.HeapAlloc.argtypes = [ctypes.c_void_p, DWORD, c_ulong]
        _k32.HeapAlloc.restype = c_void_p
        _k32.HeapFree.argtypes = [ctypes.c_void_p, DWORD, c_void_p]
        _k32.HeapFree.restype = BOOL

        _STILL_ACTIVE = 259
        _PTY_OK = True
    except Exception as _e:  # pragma: no cover
        print(f"[ai_terminal] ctypes ConPTY binding failed: {_e}")
        _PTY_OK = False


# ─── _Pty: ConPTY child process ───────────────────────────────────────────────


class _Pty:
    """A child process attached to a Windows pseudoconsole."""

    def __init__(self, argv, cwd, cols, rows, env):
        self.argv = list(argv)
        self.pid = 0
        self._hPC = None
        self._hInWrite = None      # we write input here
        self._hOutRead = None     # we read output here
        self._hProcess = None
        self._hThread = None
        self._attr_list = None
        self._heap_buf = None
        self._alive = True
        self._cmdline = " ".join(argv)
        self._cwd = cwd or None
        self._env = env
        self._cols = cols
        self._rows = rows

    def start(self):
        hPipePtyIn = HANDLE()
        hInWrite = HANDLE()
        hOutRead = HANDLE()
        hPipePtyOut = HANDLE()
        if not _k32.CreatePipe(byref(hPipePtyIn), byref(hInWrite), None, 0):
            raise OSError("CreatePipe(input) failed")
        if not _k32.CreatePipe(byref(hOutRead), byref(hPipePtyOut), None, 0):
            _k32.CloseHandle(hPipePtyIn)
            _k32.CloseHandle(hInWrite)
            raise OSError("CreatePipe(output) failed")

        hPC = HANDLE()
        hr = _k32.CreatePseudoConsole(_COORD(self._cols, self._rows),
                                      hPipePtyIn, hPipePtyOut, 0, byref(hPC))
        # The pseudoconsole now holds its own copies of the pty-side pipe ends.
        _k32.CloseHandle(hPipePtyIn)
        _k32.CloseHandle(hPipePtyOut)
        if hr & 0x80000000:
            _k32.CloseHandle(hInWrite)
            _k32.CloseHandle(hOutRead)
            raise OSError(f"CreatePseudoConsole failed: HRESULT 0x{hr & 0xffffffff:08X}")
        self._hPC = hPC.value

        # Build the proc-thread attribute list (double call: NULL -> size -> alloc -> call).
        size = c_ulong(0)
        _k32.InitializeProcThreadAttributeList(None, 1, 0, byref(size))
        heap = _k32.GetProcessHeap()
        buf = _k32.HeapAlloc(heap, 0, size.value)
        if not buf:
            raise OSError("HeapAlloc attribute list failed")
        attr = c_void_p(buf)
        if not _k32.InitializeProcThreadAttributeList(attr, 1, 0, byref(size)):
            raise OSError("InitializeProcThreadAttributeList failed")
        if not _k32.UpdateProcThreadAttribute(attr, 0, _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                                              self._hPC, sizeof(HANDLE), None, None):
            raise OSError("UpdateProcThreadAttribute failed")
        self._attr_list = attr
        self._heap_buf = (heap, buf)

        si = _STARTUPINFOEXW()
        si.StartupInfo.cb = sizeof(_STARTUPINFOEXW)
        # Force the child to take the pseudoconsole as its console rather than
        # inheriting our (redirected / console-less) std handles. Without this,
        # when the host process has no console (ST's plugin host, a piped parent),
        # the child inherits those null/redirected handles and isatty() is False
        # for every stream -- so claude falls back to --print and ollama refuses
        # the interactive picker. The PSEUDOCONSOLE attribute then overrides the
        # (null) hStd* handles with the pty console.
        si.StartupInfo.dwFlags |= _STARTF_USESTDHANDLES
        si.lpAttributeList = attr.value
        pi = _PROCESS_INFORMATION()
        cmd = ctypes.create_unicode_buffer(self._cmdline)
        cwd = ctypes.c_wchar_p(self._cwd) if self._cwd else None
        # Environment block (unicode, NUL-separated, double-NUL terminated).
        envblock = "".join(f"{k}={v}\x00" for k, v in self._env.items()) + "\x00"
        envbuf = ctypes.create_unicode_buffer(envblock)
        flags = _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT
        ok = _k32.CreateProcessW(None, cmd, None, None, False, flags,
                                 envbuf, cwd, byref(si), byref(pi))
        if not ok:
            err = ctypes.get_last_error()
            raise OSError(f"CreateProcessW failed (GetLastError {err})")
        self._hProcess = pi.hProcess
        self._hThread = pi.hThread
        self.pid = pi.dwProcessId
        self._hInWrite = hInWrite
        self._hOutRead = hOutRead

    def read(self, on_data):
        """Blocking reader loop; calls on_data(bytes) until EOF. Run on a daemon thread."""
        buf = (c_char * 8192)()
        n = DWORD(0)
        while self._alive:
            ok = _k32.ReadFile(self._hOutRead, buf, 8192, byref(n), None)
            if not ok or n.value == 0:
                break
            on_data(bytes(buf[: n.value]))
        self._alive = False

    def write(self, data):
        if not self._alive or self._hInWrite is None:
            return
        written = DWORD(0)
        _k32.WriteFile(self._hInWrite, data, len(data), byref(written), None)

    def resize(self, cols, rows):
        if not self._alive or self._hPC is None:
            return
        self._cols, self._rows = cols, rows
        _k32.ResizePseudoConsole(self._hPC, _COORD(cols, rows))

    def is_alive(self):
        if not self._alive or self._hProcess is None:
            return False
        code = DWORD(0)
        if _k32.GetExitCodeProcess(self._hProcess, byref(code)):
            if code.value != _STILL_ACTIVE:
                self._alive = False
                return False
        return self._alive

    def kill(self):
        if not self._alive:
            return
        self._alive = False
        # ClosePseudoConsole emits a final frame to hOutRead; the reader drains it
        # then sees EOF. Order matters -- see plan's ConPTY pitfalls.
        if self._hPC is not None:
            _k32.ClosePseudoConsole(self._hPC)
            self._hPC = None
        if self._hProcess is not None:
            _k32.TerminateProcess(self._hProcess, 0)
        self._close_handles()

    def _close_handles(self):
        for h in (self._hInWrite, self._hOutRead, self._hThread, self._hProcess):
            if h is not None:
                _k32.CloseHandle(h)
        self._hInWrite = self._hOutRead = self._hThread = self._hProcess = None
        if self._attr_list is not None:
            _k32.DeleteProcThreadAttributeList(self._attr_list)
            self._attr_list = None
        if self._heap_buf is not None:
            _k32.HeapFree(self._heap_buf[0], 0, self._heap_buf[1])
            self._heap_buf = None


# ─── _Screen: cursor-aware grid ──────────────────────────────────────────────
# MVP renders plain text (layout-correct, monochrome). attrs are intentionally
# omitted -- colour via coalesced add_regions is a follow-up. The layout being
# cursor-aware is what removes the Terminus gutter/width bugs.

_BLANK = " "


class _Screen:
    def __init__(self, cols, rows):
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self.x = 0
        self.y = 0
        self.grid = [[_BLANK] * self.cols for _ in range(self.rows)]
        # Scrollback: rows that scroll off the top are captured here, capped at
        # 600. Larger caps make the ST view sluggish to re-render and break
        # scroll-back; 600 is the chosen balance. Rendered above the active grid.
        self.history = collections.deque(maxlen=600)
        self.saved = (0, 0)
        self.alt_screen = False
        self.dirty = True

    def resize(self, cols, rows):
        cols, rows = max(1, cols), max(1, rows)
        new = [[_BLANK] * cols for _ in range(rows)]
        for r in range(min(rows, self.rows)):
            row = self.grid[r]
            for c in range(min(cols, self.cols)):
                new[r][c] = row[c]
        self.grid = new
        self.cols, self.rows = cols, rows
        self.x = min(self.x, cols - 1)
        self.y = min(self.y, rows - 1)
        self.dirty = True

    def reset(self):
        self.grid = [[_BLANK] * self.cols for _ in range(self.rows)]
        self.history.clear()
        self.x = self.y = 0
        self.dirty = True

    def _scroll_up(self):
        popped = self.grid.pop(0)
        self.history.append("".join(popped).rstrip())
        self.grid.append([_BLANK] * self.cols)

    def put_char(self, ch):
        if self.x >= self.cols:
            self.x = 0
            self._line_feed()
        self.grid[self.y][self.x] = ch
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
            self.grid = [[_BLANK] * self.cols for _ in range(self.rows)]
            if n == 3:
                # CSI 3J = erase scrollback (and screen); 2J leaves scrollback.
                self.history.clear()
        elif n == 0:
            for c in range(self.x, self.cols):
                self.grid[self.y][c] = _BLANK
            for r in range(self.y + 1, self.rows):
                self.grid[r] = [_BLANK] * self.cols
        elif n == 1:
            for r in range(0, self.y):
                self.grid[r] = [_BLANK] * self.cols
            for c in range(0, self.x + 1):
                self.grid[self.y][c] = _BLANK
        self.dirty = True

    def erase_line(self, n):
        row = self.grid[self.y]
        if n == 0:
            for c in range(self.x, self.cols):
                row[c] = _BLANK
        elif n == 1:
            for c in range(0, self.x + 1):
                row[c] = _BLANK
        elif n == 2:
            for c in range(self.cols):
                row[c] = _BLANK
        self.dirty = True

    def save_cursor(self):
        self.saved = (self.x, self.y)
        self.dirty = True

    def restore_cursor(self):
        self.x, self.y = self.saved
        self.x = min(self.x, self.cols - 1)
        self.y = min(self.y, self.rows - 1)
        self.dirty = True

    def snapshot(self):
        """Return scrollback history + active screen as one string (rows joined by \\n).

        Rows captured by _scroll_up are prepended above the active grid so the
        ST view can be scrolled back through them."""
        lines = list(self.history)
        for i, row in enumerate(self.grid):
            s = "".join(row)
            if i == self.y:
                # Cursor row: only rstrip the part AFTER the cursor. Claude Code
                # positions the caret with CUF (\x1b[1C) for a typed space and
                # \x08\x1b[K for backspace -- neither writes a char, so the cell
                # at the cursor is an erase-blank. A full rstrip would delete it
                # and the render clamp would pull the caret back to before the
                # space. Keep cells 0..x-1 (so the caret has a real position at
                # col x) and rstrip only the tail beyond the cursor.
                x = max(self.x, 0)
                s = s[:x].ljust(x) + s[x:].rstrip()
            else:
                s = s.rstrip()
            lines.append(s)
        # Claude Code emits U+00A0 (NBSP) in the status line to stop wrapping.
        # word_wrap is off on the Ai view, so normalize to a plain space for
        # clean display (NBSP and space are both one cell — cursor map is safe).
        return "\n".join(lines).replace("\u00a0", " ")


# ─── _Parser: minimal ANSI state machine (Claude ratatui subset) ─────────────

_GROUND, _ESC, _CSI, _OSC = 0, 1, 2, 3


class _Parser:
    def __init__(self, screen):
        self.s = screen
        self.state = _GROUND
        self.params = ""

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
                self.s.put_char(ch)
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

    def _dispatch_csi(self, final):
        priv, p = self._ints()
        s = self.s
        if final == "m":  # SGR -- consumed (colour is a follow-up)
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
                s.alt_screen = (final == "h")
            # all others consumed-and-dropped so the stream stays in sync
        # P, @, L, M, S, T, r, and any other finals: consumed-and-dropped.


# ─── _Terminal: per-view owner + registry ────────────────────────────────────

_TERMINALS = {}
_REG_LOCK = threading.Lock()


class _ProcessProxy:
    """Compat shape for modules that used Terminus .process (ai_tab_manager etc)."""

    def __init__(self, pty):
        self._pty = pty

    @property
    def argv(self):
        return self._pty.argv

    @property
    def pid(self):
        return self._pty.pid

    def isalive(self):
        return self._pty.is_alive()


class _Terminal:
    def __init__(self, view, pty, screen, parser):
        self.view = view
        self.pty = pty
        self.screen = screen
        self.parser = parser
        self.offset = 0
        self.process = _ProcessProxy(pty)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._lock = threading.Lock()
        self._render_pending = False
        self._reader = None
        self._last_cols = screen.cols
        self._last_rows = screen.rows

    @classmethod
    def from_id(cls, view_id):
        with _REG_LOCK:
            return _TERMINALS.get(view_id)

    def start(self):
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        try:
            self.pty.read(self._on_data)
        except Exception as e:
            print(f"[ai_terminal] reader error: {e}")
        finally:
            sublime.set_timeout(lambda: _vwrite(self.view, "\n[process exited]\n"), 0)

    def _on_data(self, data):
        if _DEBUG:
            _debug_log(data)
        text = self._decoder.decode(data)
        with self._lock:
            self.parser.feed(text)
        _schedule_render(self)

    def send_string(self, s):
        self.pty.write(s.encode("utf-8", errors="replace"))

    def resize(self, cols, rows):
        if cols == self._last_cols and rows == self._last_rows:
            return
        self._last_cols, self._last_rows = cols, rows
        with self._lock:
            self.screen.resize(cols, rows)
        self.pty.resize(cols, rows)

    def snapshot(self):
        with self._lock:
            return self.screen.snapshot()

    def kill(self):
        try:
            self.pty.kill()
        except Exception as e:
            print(f"[ai_terminal] kill error: {e}")


# ─── view helpers ─────────────────────────────────────────────────────────────

_VIEW_NAME = "Ai"
_VIEW_SETTING = "ai_terminal_view"
_TAG_SETTING = "ai_logger"  # so panic_dialog / ClaudeSendTab still find this view


def _vwrite(view, text):
    def _do(t=text):
        view.set_read_only(False)
        view.run_command("append", {"characters": t, "scroll_to_end": True})
    sublime.set_timeout(_do, 0)


def _terminal_view(window):
    v = window.new_file()
    v.set_name(_VIEW_NAME)
    v.set_scratch(True)
    v.settings().set("word_wrap", False)
    v.settings().set("gutter", True)
    v.settings().set("line_numbers", False)
    v.settings().set("fold_buttons", False)
    v.settings().set(_VIEW_SETTING, True)
    v.settings().set(_TAG_SETTING, True)
    # NOT read-only: on_text_command swallows insert/left_delete/right_delete/
    # move and forwards them to the PTY. Making the view read-only suppresses
    # keyboard `insert` before the listener fires, so real typing would do
    # nothing (only programmatic run_command("insert") bypasses the block).
    return v


def _measure(view):
    ex = view.viewport_extent()
    cw = view.em_width() or 7.0
    lh = view.line_height() or 18.0
    cols = max(20, int(ex[0] / cw))
    rows = max(4, int(ex[1] / lh))
    return cols, rows


# ─── debounced renderer ──────────────────────────────────────────────────────

_RENDER_MS = 40


def _schedule_render(term):
    if term._render_pending:
        return
    term._render_pending = True
    sublime.set_timeout(lambda: _do_render(term), _RENDER_MS)


def _do_render(term):
    view = term.view
    if not view or not view.is_valid():
        term._render_pending = False
        return
    # Defer while the user has a text selection (to copy/cut): the full-buffer
    # view.replace + caret re-pin would wipe it. Poll until the selection clears.
    if any(not s.empty() for s in view.sel()):
        sublime.set_timeout(lambda: _do_render(term), _RENDER_MS)
        return  # leave _render_pending True so _schedule_render doesn't double-arm
    term._render_pending = False
    if not term.screen.dirty:
        return
    # Read snapshot + TUI cursor under one lock acquisition so the caret row
    # (history offset + screen.y) matches the text we render this frame.
    with term._lock:
        text = term.screen.snapshot()
        hist = len(term.screen.history)
        cy = term.screen.y
        cx = term.screen.x
    term.screen.dirty = False
    view.run_command("ai_terminal_render", {"text": text, "cursor": [hist + cy, cx]})


# ─── debug logging ────────────────────────────────────────────────────────────

_DEBUG = bool(os.environ.get("AI_TERMINAL_DEBUG"))
_DEBUG_PATH = os.path.expanduser("~/.cache/ai_terminal")
_debug_lock = threading.Lock()


def _debug_log(data):
    try:
        os.makedirs(_DEBUG_PATH, exist_ok=True)
        with open(os.path.join(_DEBUG_PATH, "raw.log"), "ab") as f:
            with _debug_lock:
                f.write(data)
    except Exception:
        pass


# ─── view event listener: keystroke forwarding + lifecycle ───────────────────


class AiTerminalViewListener(sublime_plugin.ViewEventListener):
    @classmethod
    def is_applicable(cls, settings):
        return settings.get(_VIEW_SETTING, False)

    @classmethod
    def applies_to_primary_view_only(cls):
        return False

    def on_text_command(self, command, args):
        term = _Terminal.from_id(self.view.id())
        if term is None:
            return None
        if command == "insert":
            chars = (args or {}).get("characters", "")
            if chars:
                # Enter in ST is an insert of "\n"; TUIs expect CR.
                term.send_string("\r" if chars == "\n" else chars)
            return ("ai_terminal_noop", {})
        if command == "left_delete":
            term.send_string("\x7f")
            return ("ai_terminal_noop", {})
        if command == "right_delete":
            term.send_string("\x1b[3~")
            return ("ai_terminal_noop", {})
        if command == "move":
            by = (args or {}).get("by")
            fwd = (args or {}).get("forward", False)
            if by == "characters":
                term.send_string("\x1b[C" if fwd else "\x1b[D")
                return ("ai_terminal_noop", {})
            if by == "lines":
                term.send_string("\x1b[B" if fwd else "\x1b[A")
                return ("ai_terminal_noop", {})
        return None

    def on_close(self):
        term = _Terminal.from_id(self.view.id())
        if term is None:
            return
        with _REG_LOCK:
            _TERMINALS.pop(self.view.id(), None)
        threading.Thread(target=term.kill, daemon=True).start()


class AiTerminalKeyInterceptor(sublime_plugin.EventListener):
    """Ctrl+C -> SIGINT (\\x03), Ctrl+V -> paste into the PTY."""

    def on_text_command(self, view, command_name, args):
        if not view.settings().get(_VIEW_SETTING):
            return None
        term = _Terminal.from_id(view.id())
        if term is None:
            return None
        if command_name == "copy":  # Ctrl+C (no selection) -> interrupt
            if not view.sel() or all(s.empty() for s in view.sel()):
                term.send_string("\x03")
                return ("ai_terminal_noop", {})
        if command_name == "paste":  # Ctrl+V -> forward clipboard
            text = sublime.get_clipboard()
            if text:
                # Wrap in bracketed-paste markers so the TUI inserts the whole
                # block as one paste event; without them each newline becomes
                # an Enter and a multi-line paste auto-submits on the first line.
                term.send_string("\x1b[200~" + text + "\x1b[201~")
            return ("ai_terminal_noop", {})
        return None


# ─── commands ────────────────────────────────────────────────────────────────


def _resolve_editor_path(view):
    path = view.file_name()
    if path:
        return os.path.dirname(path)
    window = view.window()
    folders = window.folders() if window else []
    return folders[0] if folders else None


def _resolve_here_path(window, paths):
    if paths:
        path = paths[0]
        return path if os.path.isdir(path) else os.path.dirname(path)
    folders = window.folders()
    return folders[0] if folders else None


def _spawn(window, path):
    if not _PTY_OK:
        sublime.error_message("ai_terminal: Windows ConPTY unavailable (ctypes binding failed).")
        return
    view = _terminal_view(window)
    window.focus_view(view)
    cols, rows = _measure(view)
    env = dict(os.environ)
    env["CLAUDE_CODE_FORCE_INTERACTIVE"] = "1"
    argv = ["cmd", "/c", "ollama", "launch", "claude"]
    pty = _Pty(argv, path, cols, rows, env)
    try:
        pty.start()
    except Exception as e:
        sublime.error_message(f"ai_terminal: failed to start PTY:\n{e}")
        view.close()
        return
    screen = _Screen(cols, rows)
    parser = _Parser(screen)
    term = _Terminal(view, pty, screen, parser)
    with _REG_LOCK:
        _TERMINALS[view.id()] = term
    term.start()


class AiTerminalOpenHereCommand(sublime_plugin.WindowCommand):
    """Sidebar: open a Claude TUI terminal in the chosen directory."""

    def run(self, paths=None):
        path = _resolve_here_path(self.window, paths or [])
        if not path:
            sublime.status_message("Ai terminal: no folder resolved")
            return
        _spawn(self.window, path)

    def is_visible(self, paths=None):
        return True


class AiTerminalOpenInEditorCommand(sublime_plugin.TextCommand):
    """Palette / tab menu: open a Claude TUI terminal in this file's directory."""

    def run(self, edit):
        path = _resolve_editor_path(self.view)
        if not path:
            sublime.status_message("Ai terminal: no folder resolved")
            return
        window = self.view.window()
        if window:
            _spawn(window, path)


class AiTerminalSendStringCommand(sublime_plugin.TextCommand):
    """Send an arbitrary string to the PTY (terminus_send_string equivalent)."""

    def run(self, edit, string=""):
        term = _Terminal.from_id(self.view.id())
        if term:
            term.send_string(string)


# ─── key -> byte translation (ported from Terminus key.py) ───────────────────
#
# ST does NOT fire on_text_command for unbound printable keys: they take a direct
# text-input path that bypasses the command system, so typed letters never
# reached the PTY (they were inserted as stray text into the view and wiped by
# the next render). The fix is the Terminus approach: Default.sublime-keymap
# binds every printable/special key to ai_terminal_keypress (gated by
# setting.ai_terminal_view), and this command translates the key name to the
# terminal byte sequence the PTY expects.

_KEY_MAP = {
    "enter": "\r",
    "backspace": "\x7f",
    "tab": "\t",
    "space": " ",
    "escape": "\x1b",
    "down": "\x1b[B",
    "up": "\x1b[A",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[1~",
    "end": "\x1b[4~",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "delete": "\x1b[3~",
    "insert": "\x1b[2~",
    "f1": "\x1bOP",
    "f2": "\x1bOQ",
    "f3": "\x1bOR",
    "f4": "\x1bOS",
    "f5": "\x1b[15~",
    "f6": "\x1b[17~",
    "f7": "\x1b[18~",
    "f8": "\x1b[19~",
    "f9": "\x1b[20~",
    "f10": "\x1b[21~",
    "f12": "\x1b[24~",
}

_APP_MODE_KEY_MAP = {
    "down": "\x1bOB",
    "up": "\x1bOA",
    "right": "\x1bOC",
    "left": "\x1bOD",
}

_CTRL_KEY_MAP = {
    "up": "\x1b[1;5A",
    "down": "\x1b[1;5B",
    "right": "\x1b[1;5C",
    "left": "\x1b[1;5D",
    "home": "\x1b[1;5~",
    "end": "\x1b[4;5~",
    "pageup": "\x1b[5;5~",
    "pagedown": "\x1b[6;5~",
    "insert": "\x1b[2;5~",
    "delete": "\x1b[3;5~",
    "@": "\x00",
    "`": "\x00",
    "[": "\x1b",
    "{": "\x1b",
    "\\": "\x1c",
    "|": "\x1c",
    "]": "\x1d",
    "}": "\x1d",
    "^": "\x1e",
    "~": "\x1e",
    "_": "\x1f",
    "?": "\x7f",
}

_ALT_KEY_MAP = {
    "up": "\x1b[1;3A",
    "down": "\x1b[1;3B",
    "right": "\x1b[1;3C",
    "left": "\x1b[1;3D",
}

_SHIFT_KEY_MAP = {
    "up": "\x1b[1;2A",
    "down": "\x1b[1;2B",
    "right": "\x1b[1;2C",
    "left": "\x1b[1;2D",
    "tab": "\x1b[Z",
    "home": "\x1b[1;2~",
    "end": "\x1b[4;2~",
    "pageup": "\x1b[5;2~",
    "pagedown": "\x1b[6;2~",
    "insert": "\x1b[2;2~",
    "delete": "\x1b[3;2~",
}


def _get_key_code(key, application_mode=False):
    if application_mode and key in _APP_MODE_KEY_MAP:
        return _APP_MODE_KEY_MAP[key]
    if key in _KEY_MAP:
        return _KEY_MAP[key]
    return key


def _get_ctrl_key_code(key):
    key = key.lower()
    if key in _CTRL_KEY_MAP:
        return _CTRL_KEY_MAP[key]
    if len(key) == 1 and "a" <= key <= "z":
        return chr(ord(key) - ord("a") + 1)
    return _get_key_code(key)


def _get_alt_key_code(key):
    key_lo = key.lower()
    if key_lo in _ALT_KEY_MAP:
        return _ALT_KEY_MAP[key_lo]
    return "\x1b" + _get_key_code(key)


def _get_shift_key_code(key):
    key = key.lower()
    if key in _SHIFT_KEY_MAP:
        return _SHIFT_KEY_MAP[key]
    if key in _KEY_MAP:
        return _KEY_MAP[key]
    return key.upper()


def _translate_key(key, ctrl=False, alt=False, shift=False):
    if ctrl:
        return _get_ctrl_key_code(key)
    if alt:
        return _get_alt_key_code(key)
    if shift:
        return _get_shift_key_code(key)
    return _get_key_code(key)


class AiTerminalKeypressCommand(sublime_plugin.TextCommand):
    """Forward a physical key (bound in Default.sublime-keymap) to the PTY.

    ST routes unbound printable keys through a direct text-input path that
    bypasses on_text_command, so the keymap binds them to this command instead.
    """

    def run(self, edit, key="", ctrl=False, alt=False, shift=False):
        if not key:
            return
        term = _Terminal.from_id(self.view.id())
        if term is None:
            return
        # Ctrl+C / Ctrl+X with an active text selection copies/cuts it instead
        # of sending SIGINT (\x03) / cut (\x18) to the PTY. No selection ->
        # forward to the PTY (interrupt / TUI cut) as before.
        if ctrl and not alt and not shift and key in ("c", "x"):
            if any(not s.empty() for s in self.view.sel()):
                self.view.run_command("copy" if key == "c" else "cut")
                return
        code = _translate_key(key, ctrl=ctrl, alt=alt, shift=shift)
        if code:
            term.send_string(code)


class AiTerminalRenderCommand(sublime_plugin.TextCommand):
    """Replace the whole view with the current screen snapshot (main-thread)."""

    def run(self, edit, text="", cursor=None):
        view = self.view
        view.set_read_only(False)
        # Only re-pin to the bottom if the user is already near it, so scrolling
        # up to read scrollback isn't yanked back on the next 40ms render.
        vp = view.viewport_position()
        ve = view.viewport_extent()
        lh = view.line_height() or 20
        near_bottom = (vp[1] + ve[1]) >= (view.layout_extent()[1] - lh * 2)
        view.replace(edit, sublime.Region(0, view.size()), text)
        if near_bottom:
            if cursor is not None:
                # Place the ST caret at Claude Code's TUI cursor (screen.y +
                # history offset, screen.x) so it sits on the > input line you
                # are typing on, not at EOF. Clamp row/col to the view so a
                # cursor past the rstripped line end stays on the right line.
                last_row = view.rowcol(view.size())[0]
                row = min(int(cursor[0]), last_row)
                line_start = view.text_point(row, 0)
                line_end = view.line(line_start).b
                pos = min(line_start + int(cursor[1]), line_end)
                sel = view.sel()
                sel.clear()
                sel.add(sublime.Region(pos, pos))
                view.show(pos, False)
            else:
                view.run_command("move_to", {"to": "eof"})
                view.show(view.size(), False)


class AiTerminalNukeCommand(sublime_plugin.TextCommand):
    """Clear the view (terminus_nuke equivalent)."""

    def run(self, edit):
        view = self.view
        view.set_read_only(False)
        view.replace(edit, sublime.Region(0, view.size()), "")
        term = _Terminal.from_id(view.id())
        if term:
            with term._lock:
                term.screen.reset()


class AiTerminalNoopCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        pass


class AiTerminalDumpScreenCommand(sublime_plugin.TextCommand):
    """Dev: print the current screen grid + cursor to the ST console."""

    def run(self, edit):
        term = _Terminal.from_id(self.view.id())
        if not term:
            print("[ai_terminal] no terminal for this view")
            return
        with term._lock:
            print(f"[ai_terminal] cursor=({term.screen.x},{term.screen.y}) "
                  f"size=({term.screen.cols}x{term.screen.rows}) "
                  f"alt={term.screen.alt_screen}")
            for r, row in enumerate(term.screen.grid):
                print(f"  {r:2d}|{''.join(row)}|")


# ─── resize poller + lifecycle ───────────────────────────────────────────────

_POLL_MS = 750
_poll_event = threading.Event()
_poll_lock = threading.Lock()
_poll_started = False


def _ensure_poller():
    global _poll_started
    with _poll_lock:
        if _poll_started:
            return
        _poll_started = True
    threading.Thread(target=_poll_loop, daemon=True).start()


def _poll_loop():
    while True:
        try:
            with _REG_LOCK:
                items = list(_TERMINALS.items())
            for _vid, term in items:
                view = term.view
                if not view.is_valid():
                    continue
                cols, rows = _measure(view)
                if (cols, rows) != (term._last_cols, term._last_rows):
                    term.resize(cols, rows)
        except Exception as e:
            print(f"[ai_terminal] poll error: {e}")
        _poll_event.wait(_POLL_MS / 1000.0)


def plugin_loaded():
    if not _PTY_OK:
        print("[ai_terminal] ConPTY unavailable; commands will report the error.")
    _poll_event.clear()
    _ensure_poller()
    print("[ai_terminal] loaded")


def plugin_unloaded():
    with _REG_LOCK:
        items = list(_TERMINALS.items())
        _TERMINALS.clear()
    for vid, term in items:
        try:
            term.kill()
        except Exception:
            pass