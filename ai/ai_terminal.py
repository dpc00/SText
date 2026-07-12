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
import json
import os
import threading
import time
import traceback
from functools import lru_cache

import sublime
import sublime_plugin

# ─── ctypes ConPTY binding (guarded: a failure must not crash PluginLoader.py) ─────

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


class _WinptyPty:
    """A child process attached to a winpty console (symmetrical to ConPTY _Pty)."""

    def __init__(self, argv, cwd, cols, rows, env):
        self.argv = list(argv)
        self.pid = 0
        self._cwd = cwd or None
        self._env = env
        self._cols = cols
        self._rows = rows
        self._proc = None
        self._alive = True

    def start(self):
        try:
            import winpty
            # winpty.PtyProcess.spawn takes argv as a list of strings, env as a dict, and dimensions as (rows, cols)
            # Note: winpty uses (rows, cols) dimensions, while ConPTY uses (cols, rows)!
            self._proc = winpty.PtyProcess.spawn(
                self.argv,
                cwd=self._cwd,
                env=self._env,
                dimensions=(self._rows, self._cols)
            )
            self.pid = getattr(self._proc, "pid", 0)
        except Exception as e:
            print(f"[ai_terminal] winpty spawn failed: {e}")
            raise e

    def read(self, on_data):
        """Blocking reader loop; calls on_data(bytes) until EOF. Run on a daemon thread."""
        while self._alive and self._proc:
            try:
                # read up to 8192 bytes
                data = self._proc.read(8192)
                if not data:
                    break
                # winpty.PtyProcess.read might return str or bytes depending on the library build.
                if isinstance(data, str):
                    on_data(data.encode("utf-8", "ignore"))
                else:
                    on_data(data)
            except Exception:
                break
        self._alive = False

    def write(self, data):
        if not self._alive or not self._proc:
            return
        try:
            if isinstance(data, bytes):
                # winpty.PtyProcess.write expects str in some wrapper builds. Let's support both.
                try:
                    self._proc.write(data)
                except TypeError:
                    self._proc.write(data.decode("utf-8", "replace"))
            else:
                self._proc.write(data)
        except Exception:
            pass

    def resize(self, cols, rows):
        if not self._alive or not self._proc:
            return
        self._cols, self._rows = cols, rows
        try:
            # winpty setwinsize expects (rows, cols)
            self._proc.setwinsize(rows, cols)
        except Exception:
            pass

    def is_alive(self):
        if not self._alive or not self._proc:
            return False
        try:
            alive = self._proc.isalive()
            if not alive:
                self._alive = False
            return alive
        except Exception:
            self._alive = False
            return False

    def kill(self):
        if not self._alive:
            return
        self._alive = False
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None


# ─── colour: 16-colour palette + SGR attr model ──────────────────────────────
# The parser quantizes every SGR colour (16/256/truecolour) down to a 16-colour
# id and packs (fg, bg, bold, reverse) into one int per cell. The renderer maps
# each non-default cell to a scope in ai_terminal.sublime-color-scheme and
# colours it via coalesced add_regions. 0 in fg/bg means "default" (no region).
# Scope names match gen_color_scheme.py:
#   ai.fb.<fg>.<bg>   (fg, bg in 0..256; 0=default)  -- single combined family
# Claude's TUI emits truecolor (38;2;r;g;b); the parser quantizes to the xterm
# 256 palette (216-cube + 24-step gray ramp + 16 ANSI) and maps every cell to
# one ai.fb.<fg>.<bg> scope, defined over the full 257x257 matrix in the view's
# ai_terminal.sublime-color-scheme. 256-level fidelity matches Terminus, so
# muted truecolours stay muted instead of snapping to a vivid primary.

# xterm 256 palette. ANSI 0-15 use the Terminus "true_black" vivid values
# (themes/true_black.json) -- MUST match gen_color_scheme.py's _ANSI16 so
# truecolour quantization picks the same index the scheme will render.
# 16-231 cube + 232-255 gray ramp are standard xterm.
_ANSI16_RGB = [
    (0x00, 0x00, 0x00), (0xFF, 0x00, 0x00), (0x00, 0xFF, 0x00), (0xFF, 0xFF, 0x00),
    (0x00, 0x00, 0xFF), (0xFF, 0x00, 0xFF), (0x00, 0xFF, 0xFF), (0xFF, 0xFF, 0xFF),
    (0x80, 0x80, 0x80), (0xFF, 0x00, 0x00), (0x00, 0xFF, 0x00), (0xFF, 0xFF, 0x00),
    (0x00, 0x00, 0xFF), (0xFF, 0x00, 0xFF), (0x00, 0xFF, 0xFF), (0xFF, 0xFF, 0xFF),
]


def _xterm256_rgb(n):
    """xterm 256-colour index -> (r, g, b). 0-15=ANSI16, 16-231=6x6x6 cube,
    232-255 = gray ramp."""
    if n < 16:
        return _ANSI16_RGB[n]
    if n >= 232:
        v = 8 + (n - 232) * 10
        return (v, v, v)
    m = n - 16
    r, g, b = m // 36, (m // 6) % 6, m % 6
    return (0 if r == 0 else 55 + r * 40,
            0 if g == 0 else 55 + g * 40,
            0 if b == 0 else 55 + b * 40)


_XTERM256_RGB = [_xterm256_rgb(i) for i in range(256)]


@lru_cache(maxsize=10000)
def _quantize256(r, g, b):
    """Nearest of the xterm 256 palette by squared distance -> 0..255.

    Cached: the 256-step scan runs once per distinct colour, then it's an
    O(1) lookup. Matches Terminus's @lru_cache on get_closest_color. This is
    what keeps _scope_for's faint-dim path cheap -- without it the faint
    thinking text cost a _quantize256 call per cell per frame (the 2s/keystroke
    bug)."""
    best, best_d = 0, 1 << 30
    for i, (pr, pg, pb) in enumerate(_XTERM256_RGB):
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best, best_d = i, d
    return best


# Packed attr bit layout:
#   fg       bits 0-8   (0=default, 1..256 = xterm 256 index 0..255)
#   bg       bits 9-17  (0=default, 1..256 = xterm 256 index 0..255)
#   bold     bit 18    (parsed, not rendered in v2 -- no bold scope family)
#   reverse  bit 19
_FG_SHIFT, _BG_SHIFT = 0, 9
_ATTR_FG_MASK = 0x1FF
_ATTR_BG_MASK = 0x1FF << _BG_SHIFT
_BOLD = 1 << 18
_REVERSE = 1 << 19
# faint (SGR 2) is rendered by dimming the fg toward black at scope-map time --
# Claude's "thinking" reasoning text is emitted as default-fg + \x1b[2m, so a
# real terminal shows it gray; without this it falls back to default white.
# Safe because _quantize256 is @lru_cache'd: the dim lookup is O(1) per colour.
_FAINT = 1 << 20


def _attr(fg=0, bg=0, flags=0):
    return (fg << _FG_SHIFT) | (bg << _BG_SHIFT) | flags


_BG_LUMA_THRESHOLD = 100

_ANSI16_HEX = [
    "#000000", "#FF0000", "#00FF00", "#FFFF00",
    "#0000FF", "#FF00FF", "#00FFFF", "#FFFFFF",
    "#808080", "#FF0000", "#00FF00", "#FFFF00",
    "#0000FF", "#FF00FF", "#00FFFF", "#FFFFFF",
]


def _xterm_hex(i):
    if i < 16:
        return _ANSI16_HEX[i]
    if i >= 232:
        v = 8 + (i - 232) * 10
        return "#%02X%02X%02X" % (v, v, v)
    n = i - 16
    r, g, b = n // 36, (n // 6) % 6, n % 6
    return "#%02X%02X%02X" % (
        0 if r == 0 else 55 + r * 40,
        0 if g == 0 else 55 + g * 40,
        0 if b == 0 else 55 + b * 40
    )


_HEX = [None] + [_xterm_hex(i) for i in range(256)]

_SCHEME_LOCK = threading.Lock()
_REGISTERED_SCOPES = set()
_SCHEME_PATH = None
_BASE_SCHEME = {
    "name": "AI Terminal",
    "variables": {},
    "globals": {
        "background": "#000000",
        "foreground": "#FFFFFF",
        "caret": "#FFFFFF",
        "selection": "#444444",
        "line_highlight": "#0a0a0a",
        "gutter": "#000000",
        "gutter_foreground": "#808080",
    },
    "rules": []
}
_PENDING_RULES = []
_WRITE_PENDING = False


def _color_scheme_log(message):
    try:
        path = os.path.expanduser("~/data/logs/ai_terminal/color_scheme.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            t_name = threading.current_thread().name
            f.write(f"[{ts}] [{t_name}] {message}\n")
    except Exception:
        pass


def _init_dynamic_color_scheme():
    global _SCHEME_PATH, _REGISTERED_SCOPES
    try:
        _SCHEME_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_terminal.sublime-color-scheme")
        if os.path.exists(_SCHEME_PATH):
            size = os.path.getsize(_SCHEME_PATH)
            # If the file size is very large (e.g. the old precompiled 8.9MB static matrix), shrink it to the base scheme.
            # 2MB is a safe threshold to distinguish a dynamic scheme from the old static matrix.
            if size > 2000000:
                msg = f"[init] Existing color scheme is very large ({size} bytes). Overwriting with clean base scheme."
                print(f"[ai_terminal] {msg}")
                _color_scheme_log(msg)
                _save_color_scheme(_BASE_SCHEME)
            else:
                with open(_SCHEME_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    rules = data.get("rules", [])
                    for r in rules:
                        if "scope" in r:
                            _REGISTERED_SCOPES.add(r["scope"])
            msg = f"[init] Initialized. Loaded {len(_REGISTERED_SCOPES)} registered scope rules from disk ({size} bytes)."
            print(f"[ai_terminal] {msg}")
            _color_scheme_log(msg)
        else:
            _save_color_scheme(_BASE_SCHEME)
            msg = "[init] Created fresh dynamic color scheme file."
            print(f"[ai_terminal] {msg}")
            _color_scheme_log(msg)
    except Exception as e:
        msg = f"[init] ERROR: Failed to initialize dynamic color scheme: {e}"
        print(f"[ai_terminal] {msg}")
        _color_scheme_log(msg)


def _save_color_scheme(scheme_data):
    # We want to write to both the workspace path and the Packages/User path
    paths = []
    if _SCHEME_PATH:
        paths.append(_SCHEME_PATH)
    try:
        user_path = os.path.join(sublime.packages_path(), "User", "ai_terminal.sublime-color-scheme")
        if user_path not in paths:
            paths.append(user_path)
    except Exception:
        pass

    for p in paths:
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            temp_path = p + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(scheme_data, f, indent=None, separators=(",", ":"))
            os.replace(temp_path, p)
        except Exception as e:
            print(f"[ai_terminal] Error writing color scheme file to {p}: {e}")


def _register_scope_async(fg, bg):
    global _WRITE_PENDING
    scope = f"ai.fb.{fg}.{bg}"
    
    with _SCHEME_LOCK:
        if scope in _REGISTERED_SCOPES:
            return
        _REGISTERED_SCOPES.add(scope)
        _color_scheme_log(f"[register] Encountered new scope: {scope} (Memory registered count: {len(_REGISTERED_SCOPES)})")
        
        # Calculate colors
        fh = _HEX[fg] if fg < len(_HEX) else None
        bh = _HEX[bg] if bg < len(_HEX) else None
        
        # Decide background color
        if bh is None:
            bg_fill = "#000001"
        else:
            bsum = int(bh[1:3], 16) + int(bh[3:5], 16) + int(bh[5:7], 16)
            bg_fill = bh if bsum >= _BG_LUMA_THRESHOLD else "#000001"
            
        # Define both foreground and background to fully avoid the ST foreground-swap bug
        kw = {"scope": scope, "background": bg_fill, "foreground": fh or "#FFFFFF"}
            
        _PENDING_RULES.append(kw)
        
        if _WRITE_PENDING:
            return
        _WRITE_PENDING = True
        
    # Throttled / debounced to avoid write storms and ST hot-reload crashes
    sublime.set_timeout_async(_flush_pending_rules, 1000)


def _flush_pending_rules():
    global _WRITE_PENDING, _PENDING_RULES
    with _SCHEME_LOCK:
        _WRITE_PENDING = False
        if not _PENDING_RULES:
            return
        rules_to_add = list(_PENDING_RULES)
        _PENDING_RULES.clear()

    # Read the existing scheme from disk (or use BASE if we can't read it)
    scheme_data = None
    if _SCHEME_PATH and os.path.exists(_SCHEME_PATH):
        try:
            with open(_SCHEME_PATH, "r", encoding="utf-8") as f:
                scheme_data = json.load(f)
        except Exception as e:
            msg = f"[flush] ERROR: Reading color scheme for flush failed: {e}"
            print(f"[ai_terminal] {msg}")
            _color_scheme_log(msg)

    if not scheme_data:
        # Crucial safety check: if the file actually exists on disk, do NOT overwrite it
        # with a blank base scheme as that would destroy all previously compiled rules!
        if _SCHEME_PATH and os.path.exists(_SCHEME_PATH):
            msg = "[flush] CRITICAL SAFETY: Aborting write to prevent wiping existing color scheme on disk."
            print(f"[ai_terminal] {msg}")
            _color_scheme_log(msg)
            return
        scheme_data = dict(_BASE_SCHEME)
        scheme_data["rules"] = []

    # Append new rules
    scheme_data.setdefault("rules", []).extend(rules_to_add)

    # Save to disk
    _save_color_scheme(scheme_data)
    msg = f"[flush] SUCCESS: Flushed {len(rules_to_add)} dynamic rules to disk. Total rules: {len(scheme_data.get('rules', []))}"
    print(f"[ai_terminal] {msg}")
    _color_scheme_log(msg)


def _scope_for(attr):
    """Map a packed cell attr to a precompiled scope, or None for default."""
    if attr == 0:
        return None
    fg = attr & _ATTR_FG_MASK
    bg = (attr & _ATTR_BG_MASK) >> _BG_SHIFT
    if attr & _REVERSE:
        fg, bg = bg, fg
    if attr & _FAINT:
        r, g, b = _XTERM256_RGB[fg - 1] if fg else (255, 255, 255)
        fg = _quantize256(r // 2, g // 2, b // 2) + 1

    if fg == 0 and bg == 0:
        return None

    scope = f"ai.fb.{fg}.{bg}"
    if scope not in _REGISTERED_SCOPES:
        _register_scope_async(fg, bg)

    return scope


def _rstrip_cells(cells):
    """Drop trailing (space, default-attr) cells. Coloured blanks are kept so a
    row-wide background highlight survives the rstrip."""
    end = len(cells)
    while end > 0 and cells[end - 1] == (" ", 0):
        end -= 1
    return cells[:end]


# ─── plugin settings (ai_terminal.sublime-settings) ──────────────────────────
# User-tunable knobs read from a settings file so they can be changed without
# editing source: scrollback history size (the minimap-fill knob -- retune by
# eye against the minimap) and min/max terminal columns (floor/ceiling on the
# auto-sized cols). A settings-change callback swaps the live deques; the resize
# poller picks up new column bounds on its next tick (~750ms), so edits apply
# without a plugin reload (which would tear down the PTY).
_SETTINGS_NAME = "ai_terminal.sublime-settings"
_settings = None  # sublime.Settings; (re)bound in plugin_loaded

_DEFAULT_SCROLLBACK = 300
_DEFAULT_MIN_COLS = 20
_DEFAULT_LAUNCH_COMMAND = ["cmd", "/c", "ollama", "launch", "claude"]
_DEFAULT_SPAWN_ENV = {
    "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1",
    "CLAUDE_CODE_AI_TERMINAL_SENTINEL": "propagated",
}


def _scrollback_size():
    s = _settings or sublime.load_settings(_SETTINGS_NAME)
    try:
        return max(0, int(s.get("scrollback_history_size", _DEFAULT_SCROLLBACK)))
    except (TypeError, ValueError):
        return _DEFAULT_SCROLLBACK


def _force_main_screen():
    s = _settings or sublime.load_settings(_SETTINGS_NAME)
    return bool(s.get("force_main_screen", True))


def _cols_bounds():
    s = _settings or sublime.load_settings(_SETTINGS_NAME)
    try:
        mn = max(1, int(s.get("min_columns", _DEFAULT_MIN_COLS)))
    except (TypeError, ValueError):
        mn = _DEFAULT_MIN_COLS
    mx_raw = s.get("max_columns", None)
    mx = None
    if mx_raw is not None:
        try:
            mx = max(mn, int(mx_raw))
        except (TypeError, ValueError):
            mx = None
    return mn, mx


def _launch_command():
    """argv list used to spawn the terminal program. Read from the
    `launch_command` setting so the agent/gateway can be swapped (e.g. to
    `["claude"]` for direct Anthropic API, or `["opencode"]`) without editing
    the plugin. Falls back to _DEFAULT_LAUNCH_COMMAND on any shape error.
    Applied on the next _spawn (reopen the ai_terminal tab)."""
    s = _settings or sublime.load_settings(_SETTINGS_NAME)
    cmd = s.get("launch_command", _DEFAULT_LAUNCH_COMMAND)
    if not isinstance(cmd, list) or not all(isinstance(a, str) for a in cmd):
        return _DEFAULT_LAUNCH_COMMAND
    return list(cmd)


def _spawn_env():
    """Dict of env vars to apply to the spawned terminal process (merged on
    top of os.environ). Read from the `spawn_env` setting so agent-specific
    env can be swapped alongside `launch_command` without editing the plugin.
    Keys and values must be strings; falls back to _DEFAULT_SPAWN_ENV on any
    shape error. Applied on the next _spawn (reopen the ai_terminal tab)."""
    s = _settings or sublime.load_settings(_SETTINGS_NAME)
    ev = s.get("spawn_env", _DEFAULT_SPAWN_ENV)
    if not isinstance(ev, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in ev.items()
    ):
        return dict(_DEFAULT_SPAWN_ENV)
    return dict(ev)


def _settings_debug_log(message):
    try:
        path = os.path.expanduser("~/data/logs/ai_terminal/settings_debug.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            ts = time.strftime('%Y-%m-%d %H:%M:%S')
            t_name = threading.current_thread().name
            f.write(f"[{ts}] [{t_name}] {message}\n")
    except Exception:
        pass


def _on_settings_change():
    """Live-apply a settings edit: swap each live terminal's history deque to
    the new cap. Column bounds are picked up by the resize poller's next
    _measure (~750ms), so nothing to do here for cols."""
    _settings_debug_log(">>> _on_settings_change CALLED")
    try:
        cap = _scrollback_size()
        _settings_debug_log(f"Parsed new scrollback_history_size cap: {cap}")
    except Exception as e:
        _settings_debug_log(f"ERROR: _scrollback_size failed: {e}\n{traceback.format_exc()}")
        return

    with _REG_LOCK:
        terms = list(_TERMINALS.values())
    _settings_debug_log(f"Found {len(terms)} active terminal(s)")

    for t in terms:
        try:
            view_id = t.view.id() if t.view else "unknown"
            view_name = t.view.name() if t.view else "unnamed"
            _settings_debug_log(f"Processing terminal for view {view_id} ({view_name})")
            _settings_debug_log(f"Acquiring t._lock for view {view_id}...")
            with t._lock:
                _settings_debug_log(f"Acquired t._lock for view {view_id}. Calling t.screen.set_history_cap({cap})")
                t.screen.set_history_cap(cap)
                _settings_debug_log(f"Successfully returned from set_history_cap for view {view_id}")
        except Exception as e:
            msg = f"ERROR: _on_settings_change failed on terminal {t}: {e}\n{traceback.format_exc()}"
            print(f"[ai_terminal] {msg}")
            _settings_debug_log(msg)
    _settings_debug_log("<<< _on_settings_change FINISHED")


# ─── _Screen: cursor-aware grid ──────────────────────────────────────────────
# Cells carry a packed colour attr alongside the char; the renderer coalesces
# equal-attr runs into add_regions. The cursor-aware layout is what removes the
# Terminus gutter/width bugs.

_BLANK = " "


class _Screen:
    def __init__(self, cols, rows):
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self.x = 0
        self.y = 0
        self.grid = [[_BLANK] * self.cols for _ in range(self.rows)]
        # Per-cell packed colour attr, parallel to grid. 0 = default (no region).
        self.attrs = [[0] * self.cols for _ in range(self.rows)]
        # Scrollback: rows that scroll off the top are captured here. The cap
        # is a USER setting (ai_terminal.sublime-settings -> scrollback_history_size),
        # default 300, retuned by eye against the minimap. FIXED in the sense
        # that it does NOT auto-size on window resize (auto-sizing trimmed
        # history on shrink and looked buggy; a large safety deque wasn't
        # worth the memory) -- but the user can change the setting live and
        # set_history_cap swaps the deque. 300 fills the minimap at font 14 /
        # 685px viewport. Rendered above the active grid. Stored as rstripped
        # [(ch, attr), ...] cell-lists so scrollback keeps its colour (a plain
        # string would lose the attrs).
        self.history = collections.deque(maxlen=_scrollback_size())
        self.saved = (0, 0)
        self.alt_screen = False
        self.dirty = True

    def resize(self, cols, rows):
        cols, rows = max(1, cols), max(1, rows)
        new = [[_BLANK] * cols for _ in range(rows)]
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
        # history rows were captured at the OLD (wider) cols and are never
        # re-wrapped, so without this they persist as lines wider than the
        # current viewport -- making the ST layout wider than the view by
        # the width difference and producing a spurious horizontal
        # scrollbar (e.g. turning on the gutter shrank cols by 2 -> a
        # 2-char horizontal scrollbar from stale scrollback). Rstrip after
        # clipping to drop any trailing blanks revealed by the clip.
        if cols < self.cols:
            new_hist = collections.deque(maxlen=self.history.maxlen)
            for row_cells in self.history:
                new_hist.append(_rstrip_cells(row_cells[:cols]))
            self.history = new_hist
        self.cols, self.rows = cols, rows
        self.x = min(self.x, cols - 1)
        self.y = min(self.y, rows - 1)
        self.dirty = True

    def reset(self):
        self.grid = [[_BLANK] * self.cols for _ in range(self.rows)]
        self.attrs = [[0] * self.cols for _ in range(self.rows)]
        self.history.clear()
        self.x = self.y = 0
        self.dirty = True

    def set_history_cap(self, cap):
        """Swap the scrollback deque to a new maxlen, preserving contents
        (trims oldest if smaller). Called from the settings-change callback
        when the user edits scrollback_history_size -- NOT from resize, so a
        window shrink does not silently drop history (the bug that got
        auto-sizing reverted)."""
        _settings_debug_log(f"    [set_history_cap] ENTER: cap={cap}, current_maxlen={self.history.maxlen}, current_len={len(self.history)}")
        try:
            cap = max(0, int(cap))
            if cap == self.history.maxlen:
                _settings_debug_log("    [set_history_cap] RETURN: cap matches current maxlen, returning early.")
                return
            _settings_debug_log(f"    [set_history_cap] Swapping deque to new maxlen={cap}...")
            self.history = collections.deque(self.history, maxlen=cap)
            _settings_debug_log(f"    [set_history_cap] Swapped. New len={len(self.history)}, maxlen={self.history.maxlen}")
        except Exception as e:
            _settings_debug_log(f"    [set_history_cap] ERROR: {e}\n{traceback.format_exc()}")
            raise

    def _scroll_up(self):
        popped = [(self.grid[0][c], self.attrs[0][c]) for c in range(self.cols)]
        _settings_debug_log(f"    [_scroll_up] ENTER: len(history) before append={len(self.history)}")
        try:
            self.history.append(_rstrip_cells(popped))
            _settings_debug_log(f"    [_scroll_up] SUCCESS: len(history) after append={len(self.history)}")
        except Exception as e:
            _settings_debug_log(f"    [_scroll_up] ERROR during append: {e}\n{traceback.format_exc()}")
            raise
        self.grid.pop(0)
        self.attrs.pop(0)
        self.grid.append([_BLANK] * self.cols)
        self.attrs.append([0] * self.cols)

    def _scroll_down(self):
        self.grid.pop()
        self.attrs.pop()
        self.grid.insert(0, [_BLANK] * self.cols)
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
            self.grid = [[_BLANK] * self.cols for _ in range(self.rows)]
            self.attrs = [[0] * self.cols for _ in range(self.rows)]
            if n == 3:
                # CSI 3J = erase scrollback (and screen); 2J leaves scrollback.
                self.history.clear()
        elif n == 0:
            for c in range(self.x, self.cols):
                self.grid[self.y][c] = _BLANK
                self.attrs[self.y][c] = 0
            for r in range(self.y + 1, self.rows):
                self.grid[r] = [_BLANK] * self.cols
                self.attrs[r] = [0] * self.cols
        elif n == 1:
            for r in range(0, self.y):
                self.grid[r] = [_BLANK] * self.cols
                self.attrs[r] = [0] * self.cols
            for c in range(0, self.x + 1):
                self.grid[self.y][c] = _BLANK
                self.attrs[self.y][c] = 0
        self.dirty = True

    def erase_line(self, n):
        row = self.grid[self.y]
        arow = self.attrs[self.y]
        if n == 0:
            for c in range(self.x, self.cols):
                row[c] = _BLANK
                arow[c] = 0
        elif n == 1:
            for c in range(0, self.x + 1):
                row[c] = _BLANK
                arow[c] = 0
        elif n == 2:
            for c in range(self.cols):
                row[c] = _BLANK
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

        rows is a list of [(ch, attr), ...] cell-lists. Each grid row is
        rstripped of trailing default-blank cells, EXCEPT the cursor row,
        which keeps cells 0..x-1 and rstrips only the tail beyond the cursor --
        mirroring the old snapshot rstrip so the caret col stays valid. cy/cx
        are the cursor position in the rendered row space (history offset +
        grid y when history is rendered; grid y otherwise).

        NBSP normalization is left to the text builder.

        Whether scrollback history is prepended depends on the renderer mode,
        gated on self.alt_screen (set by DECSET/DECRST 1049):

        - Classic main-screen renderer (alt_screen False, forced by
          CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1): Claude Code writes
          scrolling output to the primary buffer. pyte scrolls genuinely as
          new output arrives, so history is REAL scrollback. Render
          history + grid so the full conversation is visible and ST folding
          works on real scrolling text. cy shifts by len(history) so the
          caret lands on the right line.

        - Fullscreen alt-screen renderer (alt_screen True, the default when
          the env var is unset): Claude Code paints a fixed ~rows matrix on
          the alt screen and redraws full frames in place, so any pyte scroll
          pushes a row into history spuriously (e.g. on typing). Rendering
          history + grid made the view grow past the viewport -> a "solid"
          vertical scrollbar that shifts a line and covers the bottom status
          row. Grid-only keeps the view exactly `self.rows` lines -> never
          exceeds the viewport, never scrolls, no shift.
        """
        grid_rows = []
        cy_in_grid = self.y
        cx = self.x
        for i in range(self.rows):
            srow = self.grid[i]
            arow = self.attrs[i]
            if i == self.y:
                # Cursor row: keep cells 0..x-1 verbatim, rstrip the tail. The
                # cell at the cursor is usually an erase-blank; stripping it
                # leaves the row ending at col x so the render clamp seats the
                # caret at line end (see AiTerminalRenderCommand).
                x = max(self.x, 0)
                body = [(srow[c], arow[c]) for c in range(min(x, self.cols))]
                tail = [(srow[c], arow[c]) for c in range(x, self.cols)]
                grid_rows.append(body + _rstrip_cells(tail))
            else:
                cells = [(srow[c], arow[c]) for c in range(self.cols)]
                grid_rows.append(_rstrip_cells(cells))
        if self.alt_screen:
            return grid_rows, cy_in_grid, cx
        _settings_debug_log(f"    [render_cells] ENTER: listing self.history (len={len(self.history)})...")
        try:
            hist = list(self.history)
            _settings_debug_log(f"    [render_cells] SUCCESS: listed {len(hist)} elements")
        except Exception as e:
            _settings_debug_log(f"    [render_cells] ERROR during list(self.history): {e}\n{traceback.format_exc()}")
            raise
        return hist + grid_rows, len(hist) + cy_in_grid, cx


# ─── _Parser: minimal ANSI state machine (Claude ratatui subset) ─────────────

_GROUND, _ESC, _CSI, _OSC = 0, 1, 2, 3


class _Parser:
    def __init__(self, screen):
        self.s = screen
        self.state = _GROUND
        self.params = ""
        # Current SGR state. _fg/_bg are 1-based colour ids (0=default);
        # _flags holds _BOLD/_REVERSE (other styles are parsed but not rendered).
        self._fg = 0
        self._bg = 0
        self._flags = 0

    @property
    def _cur_attr(self):
        return _attr(self._fg, self._bg, self._flags)

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
            return _quantize256(p[j + 1], p[j + 2], p[j + 3]) + 1
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
                self._flags |= _BOLD
            elif c == 7:
                self._flags |= _REVERSE
            elif c == 2:
                self._flags |= _FAINT
            elif c == 22:
                # normal intensity: clears both bold and faint
                self._flags &= ~(_BOLD | _FAINT)
            elif c == 21:
                self._flags &= ~_BOLD
            elif c == 27:
                self._flags &= ~_REVERSE
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
                row[c] = _BLANK
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
                if not _force_main_screen():
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
        # Auto-follow model (Terminus-style): scroll to the bottom to show new
        # Claude output whenever _auto_follow is True. It starts True, flips
        # False when the user scrolls up to read scrollback (detected in the
        # render by vp drifting below the position we last pinned), and
        # re-engages when the user scrolls back near the bottom or types. Fresh
        # per _spawn, so a restart opens at the prompt (bottom) instead of
        # sticking at the top showing the banner.
        self._auto_follow = True
        self._last_vp_y = 0.0
        # Asciicast v3 recording (recording patch). When recording is on,
        # start() opens a per-session .cast file and writes the v3 header;
        # _on_data / send_string / resize / kill append timed events. Off
        # (file is None) => all _cast() calls are no-ops. One file per session
        # (timestamped filename), not per day, so a resume's replay is a
        # separate recording rather than appended duplicates.
        self._cast_file = None
        self._cast_lock = threading.Lock()
        self._cast_t0 = 0.0       # session start (epoch seconds)
        self._cast_last = 0.0     # timestamp of the previous event

    @classmethod
    def from_id(cls, view_id):
        with _REG_LOCK:
            return _TERMINALS.get(view_id)

    def start(self):
        # Recording patch: asciicast v3. Recording is on if
        # AI_TERMINAL_LOG_LINES is set in the spawn_env setting OR in ST's
        # process environment (_LOG_LINES). Checked per-spawn so a settings
        # edit takes effect on the next Open Ai here... without a restart.
        # When on, open a per-session .cast file (timestamped filename so
        # each session is its own recording -- a resume's replay is a NEW
        # .cast, not appended duplicates) and write the v3 header. Events
        # are appended by _cast() from _on_data / send_string / resize / kill.
        log_on = _LOG_LINES
        if not log_on:
            try:
                log_on = bool(_spawn_env().get("AI_TERMINAL_LOG_LINES"))
            except Exception:
                pass
        if log_on:
            try:
                os.makedirs(_CAST_DIR, exist_ok=True)
                self._cast_t0 = time.time()
                self._cast_last = self._cast_t0
                fname = f"ai_{time.strftime('%Y-%m-%d_%H%M%S')}.cast"
                path = os.path.join(_CAST_DIR, fname)
                self._cast_file = open(path, "w", encoding="utf-8", newline="")
                header = {
                    "version": 3,
                    "term": {
                        "cols": int(self.screen.cols),
                        "rows": int(self.screen.rows),
                        "type": "xterm-256color",
                    },
                    "timestamp": int(self._cast_t0),
                    "title": "ai_terminal",
                    "command": " ".join(self.pty.argv) if hasattr(self.pty, "argv") else "",
                }
                self._cast_file.write(json.dumps(header) + "\n")
                self._cast_file.flush()
            except Exception as e:
                print(f"[ai_terminal] cast open failed: {e}")
                self._cast_file = None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _cast(self, code, data):
        """Asciicast v3 event: [delta, code, data]. delta is seconds since the
        previous event (relative timing -- v3 change from v2's absolute
        timestamps). One write per event under _cast_lock; flush so a crash
        doesn't truncate the file. No-op when recording is off. Caller
        passes `data` already as the right Python type: str for "o"/"i"/"x",
        "{cols}x{rows}" for "r"."""
        if self._cast_file is None:
            return
        now = time.time()
        delta = now - self._cast_last
        self._cast_last = now
        # Round to ms (v3 spec recommends error-diffusion for drift; simple
        # rounding is fine for sessions of realistic length).
        line = json.dumps([round(delta, 3), code, data])
        with self._cast_lock:
            try:
                self._cast_file.write(line + "\n")
                self._cast_file.flush()
            except Exception:
                pass

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
        # Recording patch: emit an asciicast v3 "o" (output) event for the
        # raw chunk. Logged once, here, at the stream layer -- not at
        # scroll-off -- so it is faithful to what Claude emitted and does NOT
        # duplicate on resume (a resume is a new session = new .cast file).
        # The decoder is incremental; log the decoded text so the .cast is
        # valid UTF-8 JSON (v3 wants str data, not bytes). Written outside
        # self._lock so the renderer isn't blocked on file I/O; _cast_lock
        # serializes against send_string/resize/kill writes.
        #
        # Filter out highly repetitive "Executing Hooks" status-bar repaints to
        # prevent .cast files from ballooning into hundreds of megabytes.
        if "executing hook" not in text.lower():
            self._cast("o", text)
        _schedule_render(self)

    def send_string(self, s):
        # Recording patch: emit an "i" (input) event for what the user typed.
        # Most useful event for replay -- shows exactly what was entered.
        self._cast("i", s)
        self.pty.write(s.encode("utf-8", errors="replace"))

    def resize(self, cols, rows):
        if cols == self._last_cols and rows == self._last_rows:
            return
        self._last_cols, self._last_rows = cols, rows
        with self._lock:
            self.screen.resize(cols, rows)
        self.pty.resize(cols, rows)
        # Recording patch: emit an "r" (resize) event so the .cast records
        # geometry changes for accurate replay.
        self._cast("r", f"{int(cols)}x{int(rows)}")
        # Re-render so the view text matches the new (narrower) grid right
        # away. Without this, the view keeps stale wider text until Claude
        # next emits output, and the stale wider lines show a horizontal
        # scrollbar for up to several seconds after a shrink.
        _schedule_render(self)

    def snapshot(self):
        """Return the current screen as plain text (no colour). Used by any
        external caller that just wants the visible buffer; the renderer itself
        goes through render_cells() + _build_text_and_regions for colour."""
        with self._lock:
            rows, _cy, _cx = self.screen.render_cells()
        return "\n".join("".join(ch for ch, _ in row) for row in rows)

    def kill(self):
        # Recording patch: emit an "x" (exit) event and close the .cast
        # file so the recording ends cleanly. The stream-layer "o" events
        # already captured everything Claude emitted, so there's no need
        # for a [final screen] dump -- the visible grid's content is in
        # the stream.
        if self._cast_file is not None:
            self._cast("x", "0")
            with self._cast_lock:
                try:
                    self._cast_file.close()
                except Exception:
                    pass
            self._cast_file = None
        try:
            self.pty.kill()
        except Exception as e:
            print(f"[ai_terminal] kill error: {e}")


# Preserve existing terminals on module reload so open terminal views don't "crash" (become unresponsive).
import sys as _sys
_old_mod = None
if hasattr(_sys, "_stext_old_modules") and "User.ai.ai_terminal" in _sys._stext_old_modules:
    _old_mod = _sys._stext_old_modules["User.ai.ai_terminal"]

if _old_mod is not None:
    try:
        _old_terms = getattr(_old_mod, "_TERMINALS", {})
        for _vid, _term in _old_terms.items():
            # Update the instance class dynamically to point to the new classes in this reloaded module
            _term.__class__ = _Terminal
            if hasattr(_term, "pty") and _term.pty is not None:
                _term.pty.__class__ = _Pty
            if hasattr(_term, "screen") and _term.screen is not None:
                _term.screen.__class__ = _Screen
            if hasattr(_term, "parser") and _term.parser is not None:
                _term.parser.__class__ = _Parser
            if hasattr(_term, "process") and _term.process is not None:
                _term.process.__class__ = _ProcessProxy
            _TERMINALS[_vid] = _term
        if _TERMINALS:
            print(f"[ai_terminal] Successfully recovered {len(_TERMINALS)} active terminal(s) on module reload.")
    except Exception as _re_err:
        print(f"[ai_terminal] Failed to recover active terminals on reload: {_re_err}")


# ─── view helpers ─────────────────────────────────────────────────────────────

_VIEW_NAME = "Ai"
_VIEW_SETTING = "ai_terminal_view"
_TAG_SETTING = "ai_logger"  # so panic_dialog / ClaudeSendTab still find this view


def _vwrite(view, text):
    def _do(t=text):
        view.set_read_only(False)
        view.run_command("append", {"characters": t, "scroll_to_end": True})
    sublime.set_timeout(_do, 0)


def _trigger_resize_for(vid):
    """Immediately re-measure and resize the terminal for the given view id.
    Used by the settings().add_on_change callbacks so that toggling the gutter,
    line numbers, fold buttons, or margin resizes the PTY at once instead of
    waiting up to _POLL_MS (750ms) for the poller to notice."""
    with _REG_LOCK:
        term = _TERMINALS.get(vid)
    if term is None:
        return
    view = term.view
    if not view or not view.is_valid():
        return
    try:
        cols, rows = _measure(view)
        if (cols, rows) != (term._last_cols, term._last_rows):
            term.resize(cols, rows)
    except Exception as e:
        print(f"[ai_terminal] on_change resize error: {e}")


def _next_ai_name(window, prefix=None):
    """Return a unique Ai tab name for the window: 'prefix', then 'prefix 2', ...
    Distinct view.name() per tab so send_to_view (and other name-based tools) can
    target a specific Ai tab instead of hitting the ambiguous 'Ai' every tab had
    when _VIEW_NAME was hardcoded."""
    used = set()
    for v in window.views():
        if v.settings().get(_VIEW_SETTING, False):
            used.add(v.name())
    pfx = prefix or _VIEW_NAME
    if pfx not in used:
        return pfx
    n = 2
    while f"{pfx} {n}" in used:
        n += 1
    return f"{pfx} {n}"


def _terminal_view(window, name=None):
    v = window.new_file()
    v.set_name(name or _next_ai_name(window))
    v.set_scratch(True)
    v.settings().set("word_wrap", False)
    v.settings().set("gutter", True)
    v.settings().set("line_numbers", True)
    v.settings().set("fold_buttons", True)
    # margin=0 on the terminal view: the right margin is "scrollable" in ST
    # (the horizontal scroll range grows 1px per 1px of margin), so any
    # nonzero margin shows up as a horizontal scrollbar. Terminals don't need
    # text padding anyway. See _measure for the width calc.
    v.settings().set("margin", 0)
    # draw_centered=False and a pinned scroll_past_end=False isolate the
    # terminal from the user's global scroll_past_end preference (which they
    # may enable for code views). A fixed-height TUI has no use for
    # scroll-past-end anyway. NOTE: is_widget=True was tried (matching
    # Terminus) to stop ST's on-activate viewport reposition, but it makes ST
    # hide the main menu while the terminal is focused -- unacceptable, so it
    # is NOT set.
    v.settings().set("draw_centered", False)
    v.settings().set("scroll_past_end", False)
    v.settings().set(_VIEW_SETTING, True)
    v.settings().set(_TAG_SETTING, True)
    # Instant resize on gutter / line_numbers / fold_buttons / margin toggles.
    # add_on_change fires on the main thread right after the setting changes,
    # but viewport_extent() may not yet reflect the new gutter width (ST lays
    # out asynchronously), so defer the measure+resize to the next main-thread
    # tick. Without this, the poller catches the change up to 750ms later and
    # the TUI keeps the old column count (text gets truncated / scrollbars
    # appear) for that lag.
    vid = v.id()

    def _on_layout_setting_change():
        sublime.set_timeout(lambda: _trigger_resize_for(vid), 0)

    for _key in ("gutter", "line_numbers", "fold_buttons", "margin"):
        v.settings().add_on_change(_key, _on_layout_setting_change)
    # Dedicated colour scheme: defines the ai.fg/bg/fb.* scopes the renderer
    # maps cells to (see gen_color_scheme.py). Scoped to this view only, so the
    # rest of the editor keeps the user's theme. find_resources (plural) returns
    # the installed path; fall back to the canonical Packages/User path.
    try:
        hits = sublime.find_resources("ai_terminal.sublime-color-scheme")
        if hits:
            v.settings().set("color_scheme", hits[0])
        else:
            v.settings().set("color_scheme",
                             "Packages/User/ai_terminal.sublime-color-scheme")
    except Exception:
        v.settings().set("color_scheme",
                         "Packages/User/ai_terminal.sublime-color-scheme")
    # NOT read-only: on_text_command swallows insert/left_delete/right_delete/
    # move and forwards them to the PTY. Making the view read-only suppresses
    # keyboard `insert` before the listener fires, so real typing would do
    # nothing (only programmatic run_command("insert") bypasses the block).
    return v


def _measure(view):
    ex = view.viewport_extent()
    cw = view.em_width() or 7.0
    lh = view.line_height() or 18.0
    # Width math: three things eat horizontal space -- the gutter (line
    # numbers), the fold buttons, and the `margin` setting. viewport_extent
    # already excludes the gutter + fold buttons (they live left of the
    # viewport -- confirmed: cols drops when line_numbers/fold_buttons turn
    # on). `margin` is padding INSIDE the viewport (left + right of the text),
    # so it must be subtracted here, otherwise cols is overestimated by the
    # margin. margin may be an int (all sides) or [left, top, right, bottom].
    # The terminal view sets margin=0 (see _terminal_view) so this is normally
    # a no-op, but keep it for safety in case a setting toggles margin back on.
    margin = view.settings().get("margin", 0) or 0
    if isinstance(margin, (list, tuple)):
        ml = margin[0] if len(margin) > 0 else 0
        mr = margin[2] if len(margin) > 2 else ml
    else:
        ml = mr = margin
    usable_w = ex[0] - ml - mr
    # word_wrap=False: ST's horizontal scroll range is (maxlen + 3) * cw -
    # viewport (clamped at 0), NOT content overflow. The +3 = +1 end-of-line
    # caret position (ST's layout_extent is (longest_line + 1) * cw) + ~2 chars
    # of ST end-of-line padding that count toward the scroll range even when
    # text fits. So cols = int(usable_w / cw) - 3 is required for a zero
    # horizontal scrollbar; -2 or less still leaves a 1-char sliver.
    #
    # We deliberately do NOT use word_wrap=True to shrink the gap: word_wrap
    # kills the horizontal scrollbar, yes, but it makes any single line that
    # lands at the wrap threshold (box-drawing chars render slightly wider
    # than em_width, a stale wide line left over from a shrink, or a
    # transient overshoot during a TUI frame) soft-wrap to a new visual row,
    # and one extra row = a full line-height of vertical scroll bar. That is
    # more disruptive than the ~3c right gap we pay here. The gap is the cost
    # of a non-wrapping terminal view with zero scrollbars.
    # Floor/ceiling from settings (ai_terminal.sublime-settings ->
    # min_columns / max_columns). The -3 no-horizontal-scrollbar math still
    # applies inside these bounds; max_columns=null means no ceiling.
    mn, mx = _cols_bounds()
    cols = max(mn, int(usable_w / cw) - 3)
    if mx is not None:
        cols = min(mx, cols)
    # Subtract 1 row for a vertical safety margin: int(ex[1]/lh) fills the
    # viewport EXACTLY (content_h == viewport_h), and ST shows a "solid"
    # vertical scrollbar (thumb fills the track, won't move) whenever
    # layout_extent >= viewport -- even when they're exactly equal. Whether
    # that happens depends on where the TUI's current frame lands on the row
    # boundary, so the bar appeared intermittently ("sometimes solid, won't
    # move"). The -1 guarantees content_h < viewport_h by one line, so no
    # vertical scrollbar ever appears.
    #
    # The blank line(s) sometimes visible at the top of the view are NOT from
    # this calc: ST itself reserves a 1-line top margin, and Claude's TUI
    # independently leaves text line 1 (and sometimes line 2) blank. Those
    # stack; neither is ai_terminal's doing.
    rows = max(4, int(ex[1] / lh) - 1)
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
    # Read structured cells + TUI cursor under one lock acquisition so the caret
    # row (history offset + screen.y) matches the text we render this frame.
    with term._lock:
        rows, cy, cx = term.screen.render_cells()
    term.screen.dirty = False
    text, regions = _build_text_and_regions(rows)
    view.run_command("ai_terminal_render",
                     {"text": text, "cursor": [cy, cx], "regions": regions})


def _build_text_and_regions(rows):
    """Flatten structured rows into the view text + a list of [begin, end, scope]
    colour regions. Adjacent cells whose attr maps to the same scope are
    coalesced into one region so add_regions stays cheap. NBSP (U+00A0) that
    Claude emits to stop wrapping is normalized to a plain space here."""
    parts = []
    regs = []
    offset = 0
    for cells in rows:
        run_scope = None
        run_start = -1
        for ch, attr in cells:
            parts.append(ch)
            scope = _scope_for(attr) if attr else None
            if scope != run_scope:
                if run_scope is not None:
                    regs.append([run_start, offset, run_scope])
                run_scope = scope
                run_start = offset
            offset += 1
        if run_scope is not None:
            regs.append([run_start, offset, run_scope])
        parts.append("\n")
        offset += 1
    if parts and parts[-1] == "\n":
        parts.pop()
    text = "".join(parts).replace(" ", " ")
    return text, regs


# Per-view set of colour region keys added last frame, so we can erase stale
# scopes (whose cells scrolled away or changed attr) on the next render.
_LAST_COLOR_KEYS = {}
_COLOR_KEY_PREFIX = "ai_term_c_"


def _apply_color_regions(view, regs):
    """Group regions by scope and add them; erase any scope keys we added last
    frame but did not re-add this frame, so stale colour doesn't linger."""
    by_scope = {}
    for begin, end, scope in regs:
        by_scope.setdefault(scope, []).append(sublime.Region(begin, end))
    used = set()
    for scope, rs in by_scope.items():
        key = _COLOR_KEY_PREFIX + scope
        # The scheme gives every ai.fb.* scope a solid #000001 background
        # (off-by-one from the view's #000000 global bg -- ST collapses a rule
        # bg that EQUALS the global bg to None, which re-triggers the swap; so
        # #000001, visually indistinguishable from pure black, is used) plus
        # the text colour as foreground. ST's add_regions only colours the
        # TEXT when the scope defines BOTH fg and a SOLID bg; with only fg it
        # swaps, painting the fg as the fill and leaving the text default. So
        # we keep the fill (DRAW_NO_OUTLINE, no DRAW_NO_FILL): the #000001 fill
        # is invisible and the foreground renders on the text. DRAW_NO_OUTLINE:
        # no border around the run.
        view.add_regions(key, rs, scope=scope, flags=sublime.DRAW_NO_OUTLINE)
        used.add(key)
    vid = view.id()
    last = _LAST_COLOR_KEYS.get(vid, ())
    for k in last:
        if k not in used:
            view.erase_regions(k)
    _LAST_COLOR_KEYS[vid] = used


# ─── debug logging ────────────────────────────────────────────────────────────

_DEBUG = bool(os.environ.get("AI_TERMINAL_DEBUG"))
_DEBUG_PATH = r"C:\Users\donal\data\logs\ai_terminal_raw_ansi_stream_debug_logs"
_debug_lock = threading.Lock()
# Asciicast v3 recording (recording patch): env-gated like _DEBUG. When on
# (AI_TERMINAL_LOG_LINES set in spawn_env OR in ST's process env), each
# _Terminal.start() opens a per-session .cast file under descriptive directory
# and writes the v3 header; _on_data/send_string/resize/kill append
# timed events. Recording at the stream layer (not scroll-off) means it is
# faithful to what Claude emitted and does NOT duplicate on resume (a resume
# is a new session = new .cast). Per stext-settings-json-strict the toggle is
# NOT a top-level setting key; it lives in spawn_env where the user put it.
_LOG_LINES = bool(os.environ.get("AI_TERMINAL_LOG_LINES"))
_CAST_DIR = r"C:\Users\donal\data\logs\ai_terminal_asciinema_casts_for_troubleshooting_rendering"


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
                term._auto_follow = True
                _scroll_to_bottom(self.view)
                term._last_vp_y = self.view.viewport_position()[1]
                # Enter in ST is an insert of "\n"; TUIs expect CR.
                term.send_string("\r" if chars == "\n" else chars)
            return ("ai_terminal_noop", {})
        if command == "left_delete":
            term._auto_follow = True
            _scroll_to_bottom(self.view)
            term._last_vp_y = self.view.viewport_position()[1]
            term.send_string("\x7f")
            return ("ai_terminal_noop", {})
        if command == "right_delete":
            term._auto_follow = True
            _scroll_to_bottom(self.view)
            term._last_vp_y = self.view.viewport_position()[1]
            term.send_string("\x1b[3~")
            return ("ai_terminal_noop", {})
        if command == "move":
            by = (args or {}).get("by")
            fwd = (args or {}).get("forward", False)
            if by == "characters":
                term._auto_follow = True
                _scroll_to_bottom(self.view)
                term._last_vp_y = self.view.viewport_position()[1]
                term.send_string("\x1b[C" if fwd else "\x1b[D")
                return ("ai_terminal_noop", {})
            if by == "lines":
                term._auto_follow = True
                _scroll_to_bottom(self.view)
                term._last_vp_y = self.view.viewport_position()[1]
                term.send_string("\x1b[B" if fwd else "\x1b[A")
                return ("ai_terminal_noop", {})
        return None

    def on_modified(self):
        # Catch programmatic inserts that bypass on_text_command (e.g.
        # send_to_view's run_command("insert") from another plugin, IME/unicode
        # input, paste). on_text_command does NOT fire for these, so without
        # this handler they'd land in the buffer and get wiped on the next
        # render without ever reaching the PTY -- which is why send_to_view
        # worked on Terminus tabs but not here. Mirrors Terminus's
        # event_listeners.on_modified: read command_history(0), forward "insert"
        # chars to the PTY, skip own commands. Unlike Terminus we do NOT
        # soft_undo others -- the full-view replace in ai_terminal_render wipes
        # stray text within a frame, and soft_undo risks recursion / clobbering
        # other plugins' writes to this view. ViewEventListener.on_modified
        # takes only self (view is self.view), unlike Terminus's plain
        # EventListener which takes a view arg.
        view = self.view
        term = _Terminal.from_id(view.id())
        if term is None or not term.pty.is_alive():
            return
        try:
            command, args, _ = view.command_history(0)
        except Exception:
            return
        if not command:
            return
        # skip our own commands (ai_terminal_render replaces the whole view;
        # ai_terminal_send_string/keypress already wrote to the PTY) and the
        # "[process exited]" append marker, plus undo machinery to avoid loops
        if (command.startswith("ai_terminal")
                or command in ("append", "soft_undo", "undo", "redo")):
            return
        if command == "insert" and isinstance(args, dict) and "characters" in args:
            chars = args["characters"]
            if chars and len(view.sel()) == 1 and view.sel()[0].empty():
                term._auto_follow = True
                _scroll_to_bottom(view)
                term._last_vp_y = view.viewport_position()[1]
                # Forward raw. \n submits in Claude Code's TUI (a pasted multi-line
                # block becomes multi-prompt, one submit per line); converting a
                # lone \n to \r would NOT submit (verified) -- so send \n as-is.
                term.send_string(chars)

    def on_close(self):
        term = _Terminal.from_id(self.view.id())
        if term is None:
            return
        with _REG_LOCK:
            _TERMINALS.pop(self.view.id(), None)
        threading.Thread(target=term.kill, daemon=True).start()

    # ─── pre-empt ST's internal view.show on focus/hover ───────────────────
    #
    # ST's compositor repaints the view on Windows activation messages
    # (WM_ACTIVATE / WM_KILLFOCUS) and on hover, and briefly paints at a stale
    # viewport position even though vp is (0,0). The continuous clamp loop
    # catches the resulting vp drift within ~16ms (1 frame), but the user sees
    # that 1 frame. These handlers run BEFORE ST's internal repaint on the same
    # event, so clamping vp here pre-empts the bad paint instead of waiting for
    # the 16ms tick. Only fires when content fits within 1 line of overflow, so
    # it never fights the user scrolling up to read scrollback.
    def _preclamp_vp(self):
        v = self.view
        try:
            if not v or not v.is_valid():
                return
            le = v.layout_extent()
            ve = v.viewport_extent()
            vp = v.viewport_position()
            lh = v.line_height() or 12.0
            if le[1] - ve[1] <= lh and (vp[0] != 0.0 or vp[1] != 0.0):
                v.set_viewport_position((0.0, 0.0), False)
        except Exception:
            pass

    def on_hover(self, point, hover_zone):
        self._preclamp_vp()

    def on_activated(self):
        self._preclamp_vp()

    def on_deactivated(self):
        self._preclamp_vp()


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


def _spawn(window, path, profile=None):
    if not _PTY_OK:
        sublime.error_message("ai_terminal: Windows ConPTY unavailable (ctypes binding failed).")
        return

    s = sublime.load_settings(_SETTINGS_NAME)
    profiles = s.get("profiles", {})
    
    profile_name = profile
    if not profile_name:
        profile_name = s.get("default_profile")
        
    profile_data = profiles.get(profile_name) if profile_name else None
    
    if profile_data and isinstance(profile_data, dict):
        argv = profile_data.get("launch_command", _DEFAULT_LAUNCH_COMMAND)
        extra_env = profile_data.get("spawn_env", {})
    else:
        # Fallback to legacy single command settings
        argv = _launch_command()
        extra_env = _spawn_env()
        profile_name = "Legacy" if profile_name else None

    # Determine unique tab name
    pfx = "Ai"
    if profile_name:
        if "Gemini" in profile_name:
            pfx = "Gemini"
        elif "Claude" in profile_name:
            pfx = "Claude"
        else:
            pfx = profile_name
    tab_name = _next_ai_name(window, prefix=pfx)

    view = _terminal_view(window, name=tab_name)
    window.focus_view(view)
    cols, rows = _measure(view)
    
    env = dict(os.environ)
    env.update(extra_env)

    backend = s.get("windows_pty_backend", "conpty")
    if backend == "winpty":
        try:
            pty = _WinptyPty(argv, path, cols, rows, env)
            print("[ai_terminal] Spawning PTY process using 'winpty' backend.")
        except Exception as e:
            sublime.error_message(f"ai_terminal: failed to start Winpty PTY:\n{e}")
            view.close()
            return
    else:
        pty = _Pty(argv, path, cols, rows, env)
        print("[ai_terminal] Spawning PTY process using 'conpty' backend.")

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
    """Open a Claude TUI terminal in the chosen directory.

    Menu: Side Bar.sublime-menu — "Open Ai Terminal here..."
    Command palette: "Ai: Open Terminal Here"
    """

    def run(self, paths=None, profile=None):
        path = _resolve_here_path(self.window, paths or [])
        if not path:
            sublime.status_message("Ai terminal: no folder resolved")
            return
        _spawn(self.window, path, profile=profile)

    def is_visible(self, paths=None):
        return True


class AiTerminalOpenInEditorCommand(sublime_plugin.TextCommand):
    """Open a Claude TUI terminal in this file's directory.

    Menu: Context.sublime-menu / Tab Context.sublime-menu — "Open Ai Terminal here..."
    Command palette: "Ai: Open Terminal in Editor"
    """

    def run(self, edit, profile=None):
        path = _resolve_editor_path(self.view)
        if not path:
            sublime.status_message("Ai terminal: no folder resolved")
            return
        window = self.view.window()
        if window:
            _spawn(window, path, profile=profile)


class AiTerminalSelectProfileCommand(sublime_plugin.WindowCommand):
    """Show a Quick Panel to pick and open a terminal profile.

    Command palette: "Ai: Open Terminal Profile..."
    """

    def run(self, paths=None):
        s = sublime.load_settings(_SETTINGS_NAME)
        profiles = s.get("profiles", {})
        profile_names = list(profiles.keys())

        if not profile_names:
            # Fall back to launching default terminal
            self.window.run_command("ai_terminal_open_here", {"paths": paths})
            return

        def on_done(idx):
            if idx == -1:
                return
            self.window.run_command("ai_terminal_open_here", {
                "profile": profile_names[idx],
                "paths": paths
            })

        self.window.show_quick_panel(profile_names, on_done)


class AiTerminalSendStringCommand(sublime_plugin.TextCommand):
    """Send an arbitrary string to the PTY (terminus_send_string equivalent).

    No key/menu/palette binding; invoked programmatically.
    """

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


def _scroll_to_bottom(view):
    """Jump the viewport to the bottom of the content so the prompt line +
    caret are visible. Called on user input so typing brings the user back to
    the prompt after scrolling up to read scrollback (standard terminal
    behavior). No-op when content fits the viewport (vp is already at 0)."""
    le = view.layout_extent()
    ve = view.viewport_extent()
    if le[1] > ve[1]:
        view.set_viewport_position((0.0, le[1] - ve[1]), False)


class AiTerminalKeypressCommand(sublime_plugin.TextCommand):
    """Forward a physical key to the PTY as the terminal byte sequence it expects.

    ST routes unbound printable keys through a direct text-input path that
    bypasses on_text_command, so the keymap binds them to this command
    instead. Every printable/special key is bound in Default.sublime-keymap
    (letters, digits, punctuation, arrows, enter, tab, space, backspace,
    insert/delete, pageup/pagedown, home/end, escape, and ctrl/alt/shift
    combinations of same), all gated by context setting.ai_terminal_view ==
    true; args carry the key name and modifier flags.

    No menu/palette entry.
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
            term._auto_follow = True
            _scroll_to_bottom(self.view)
            term._last_vp_y = self.view.viewport_position()[1]
            term.send_string(code)


class AiTerminalRenderCommand(sublime_plugin.TextCommand):
    """Replace the whole view with the current screen snapshot on the main thread.

    No key/menu/palette binding; invoked programmatically.
    """

    def run(self, edit, text="", cursor=None, regions=None):
        view = self.view
        view.set_read_only(False)
        # Only re-pin to the bottom if the user is already near it, so scrolling
        # up to read scrollback isn't yanked back on the next 40ms render.
        vp = view.viewport_position()
        ve = view.viewport_extent()
        lh = view.line_height() or 20
        near_bottom = (vp[1] + ve[1]) >= (view.layout_extent()[1] - lh * 2)
        view.replace(edit, sublime.Region(0, view.size()), text)
        # Re-apply colour regions every frame: view.replace invalidates the old
        # regions, and add_regions with the same key replaces what was there.
        _apply_color_regions(view, regions or [])
        # Always reposition the ST caret at Claude Code's TUI cursor (screen.y +
        # history offset, screen.x) so it sits on the > input line you are
        # typing on, not at EOF -- even when the user has scrolled up to read
        # scrollback (the caret is then off-screen below, correct in content
        # but not yanked into view). Only scroll to show it when the user is
        # already near the bottom, so scrolling up to read scrollback isn't
        # yanked back on the next 40ms render. User input handlers explicitly
        # scroll to the bottom before forwarding the key (see
        # _scroll_to_bottom), so typing brings the viewport back to the prompt
        # -- standard terminal behavior.
        content_fits = view.layout_extent()[1] <= ve[1] + 0.5
        # Auto-follow (Terminus-style): scroll to the bottom on new Claude
        # output when _auto_follow is True. The flag flips False when the user
        # scrolls up to read scrollback (vp drifts below where we last pinned)
        # and re-engages when they scroll back near the bottom or type. This
        # replaces the old near_bottom-only gate, which never followed on a
        # fresh restart (vp starts at the top so near_bottom was False) and
        # stopped following the moment the viewport drifted a couple of lines
        # above the bottom -- so Claude's output appeared below the fold and
        # the user said "our terminal is not listening to Claude."
        term = _Terminal.from_id(view.id())
        if term is not None:
            if vp[1] < term._last_vp_y - lh * 1.5:
                term._auto_follow = False
            if near_bottom:
                term._auto_follow = True
        do_follow = (term is not None and term._auto_follow) if term is not None else near_bottom
        if cursor is not None:
            last_row = view.rowcol(view.size())[0]
            row = min(int(cursor[0]), last_row)
            line_start = view.text_point(row, 0)
            line_end = view.line(line_start).b
            pos = min(line_start + int(cursor[1]), line_end)
            sel = view.sel()
            sel.clear()
            sel.add(sublime.Region(pos, pos))
            # Pin to the exact bottom (not view.show's "nice" position) when
            # following and content exceeds the viewport. When content fits,
            # the caret is always visible and we must NOT call show -- ST
            # pushes vp to a negative "nice" position and the clamp yanks it
            # back, which the user sees as a 1-line up/down shift on every TUI
            # frame. Skipping show() when content fits eliminates the shift;
            # the clamp below pins vp to 0.
            if do_follow and not content_fits:
                _scroll_to_bottom(view)
                if term is not None:
                    term._last_vp_y = view.viewport_position()[1]
        else:
            view.run_command("move_to", {"to": "eof"})
            if do_follow and not content_fits:
                _scroll_to_bottom(view)
                if term is not None:
                    term._last_vp_y = view.viewport_position()[1]
        if content_fits:
            view.set_viewport_position((0.0, 0.0), False)


class AiTerminalNukeCommand(sublime_plugin.TextCommand):
    """Clear the view and reset the terminal screen (terminus_nuke equivalent).

    Key binding: ctrl+alt+k (context: setting.ai_terminal_view == true).
    Menu: Main.sublime-menu → Tools → Ai Utilities — "Nuke Ai Terminal".
    Command palette: "Ai: Nuke Ai Terminal".
    """

    def is_enabled(self):
        # Gate so the menu item greys out outside an ai_terminal view —
        # run() would otherwise blank any active file view.
        return bool(self.view.settings().get("ai_terminal_view"))

    def run(self, edit):
        view = self.view
        view.set_read_only(False)
        view.replace(edit, sublime.Region(0, view.size()), "")
        term = _Terminal.from_id(view.id())
        if term:
            with term._lock:
                term.screen.reset()


class AiTerminalNoopCommand(sublime_plugin.TextCommand):
    """Do nothing (placeholder no-op command).

    No key/menu/palette binding; invoked programmatically.
    """

    def run(self, edit):
        pass


class AiTerminalDumpScreenCommand(sublime_plugin.TextCommand):
    """Print the current screen grid and cursor to the ST console for debugging.

    No key/menu/palette binding; invoked programmatically (debug).
    """

    def run(self, edit):
        term = _Terminal.from_id(self.view.id())
        if not term:
            print("[ai_terminal] no terminal for this view")
            return
        with term._lock:
            print(f"[ai_terminal] cursor=({term.screen.x},{term.screen.y}) "
                  f"size=({term.screen.cols}x{term.screen.rows}) "
                  f"alt={term.screen.alt_screen} "
                  f"sgr=fg={term.parser._fg} bg={term.parser._bg} "
                  f"flags={term.parser._flags}")
            for r, row in enumerate(term.screen.grid):
                ar = term.screen.attrs[r]
                marks = "".join("*" if a else " " for a in ar)
                print(f"  {r:2d}|{''.join(row)}|")
                print(f"     {marks}|  (attrs: * = non-default)")


# ─── resize poller + lifecycle ───────────────────────────────────────────────

_POLL_MS = 750
_poll_token = None


def _ensure_poller():
    global _poll_token
    if _poll_token is not None:
        return
    _poll_loop()


def _poll_loop():
    global _poll_token
    _poll_token = None
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
    _poll_token = sublime.set_timeout(_poll_loop, _POLL_MS)


# ─── viewport clamp ───────────────────────────────────────────────────────────
#
# ST's view.show() overshoots to a NEGATIVE viewport y (e.g. vp[1]=-20) when
# content fits the viewport -- it tries to "nicely" position the caret and
# overshoots because there's nothing to scroll. Our own render clamps this, but
# ST ALSO calls view.show internally on view focus/hover -- mouse entering the
# view bbox triggers it BETWEEN renders. During generation a render clamps it
# within ~110ms, but when Claude is idle there's no TUI output -> no render ->
# the -20 persists until the next TUI frame (cursor blink ~500ms), so the user
# sees the text dip one line for ~500ms then snap back. This loop clamps vp to
# (0,0) whenever content fits, independent of the render clock, killing the dip
# within 16ms. It only fires when content fits (le <= ve), so it never fights
# the user scrolling up to read scrollback when content exceeds the viewport.

_clamp_token = None


def _clamp_vp_loop():
    global _clamp_token
    try:
        for _vid, term in list(_TERMINALS.items()):
            v = term.view
            if not v or not v.is_valid():
                continue
            le = v.layout_extent()
            ve = v.viewport_extent()
            vp = v.viewport_position()
            lh = v.line_height() or 12.0
            # Tolerate small overflow: when ve briefly shrinks (ST shows a transient
            # bar / the TUI emits a shorter frame), le can exceed ve by a few px and
            # the strict `le <= ve + 0.5` condition fails, leaving vp drifted to
            # e.g. (0, 4) for ~200ms until ve recovers -- the user sees a long
            # down-dip. Clamping vp=0 whenever content exceeds the viewport by <= 1
            # line height kills that dip (at most the bottom ~1 line is briefly
            # clipped, which is far less jarring than a 200ms shift). When content
            # exceeds by more (the user scrolled up to read scrollback), do NOT
            # clamp -- let vp stay where the user scrolled.
            if le[1] - ve[1] <= lh and (vp[0] != 0.0 or vp[1] != 0.0):
                v.set_viewport_position((0.0, 0.0), False)
    except Exception as e:
        print(f"[ai_terminal] clamp loop error: {e}")
    _clamp_token = sublime.set_timeout(_clamp_vp_loop, 16)


def plugin_loaded():
    if not _PTY_OK:
        print("[ai_terminal] ConPTY unavailable; commands will report the error.")
    _ensure_poller()
    global _clamp_token, _settings
    _init_dynamic_color_scheme()
    # Bind the settings object and live-apply edits (the callback fires on the
    # main thread right after a settings file write).
    _settings = sublime.load_settings(_SETTINGS_NAME)
    _settings.add_on_change("ai_terminal", _on_settings_change)
    if _clamp_token:
        try:
            sublime.cancel_timeout(_clamp_token)
        except Exception:
            pass
    _clamp_token = sublime.set_timeout(_clamp_vp_loop, 16)
    print("[ai_terminal] loaded")


def plugin_unloaded():
    global _clamp_token, _settings, _poll_token
    if _settings is not None:
        try:
            _settings.clear_on_change("ai_terminal")
        except Exception:
            pass
    if _clamp_token:
        try:
            sublime.cancel_timeout(_clamp_token)
        except Exception:
            pass
        _clamp_token = None
    if _poll_token:
        try:
            sublime.cancel_timeout(_poll_token)
        except Exception:
            pass
        _poll_token = None
    # Deliberately do NOT kill ConPTY children on unload.  The terminal
    # process may be opencode itself (or another long-running CLI agent);
    # killing it here means a plugin reload triggered by the agent's own
    # file deployment will murder the agent mid-session — an unrecoverable
    # crash with no error log.  The children are owned by this ST instance
    # and will be cleaned up when ST itself exits.
