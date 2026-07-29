"""ai_terminal.py -- bare-bones owned terminal for the Claude CLI.

Replaces the Terminus dependency for AI launch. No third-party packages: pure
ctypes against the Windows ConPTY (Pseudoconsole) API, plus a small cursor-aware
ANSI renderer tailored to the subset Claude's ratatui TUI emits. Because all of
the rendering/state code is ours, every bug is fixable here.

Architecture:
  ai/terminal/  -- pure core (Screen, Parser, colours, keys, render) — unit-testable
  _Pty          -- ConPTY wrapper (ctypes). Spawns the child, gives us a byte stream.
  _Terminal     -- owns a _Pty + Screen + Parser; registry keyed by view id.
  renderer      -- debounced, walks Screen -> view text on the main thread.
  listener      -- forwards keystrokes from the view to the PTY; kills PTY on close.

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
import shutil
import subprocess
import sys
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

        # use_last_error=True so ctypes.get_last_error() works after CreateProcessW.
        # windll.kernel32 does not preserve last-error (reports 0 on failure).
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
        # list2cmdline quotes paths with spaces; plain " ".join does not and
        # also cannot launch npm .cmd shims without prior _resolve_launch_argv.
        self._cmdline = subprocess.list2cmdline(self.argv)
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
            raise OSError(
                f"CreateProcessW failed (GetLastError {err}) for cmdline: {self._cmdline!r}"
            )
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
                except (TypeError, AttributeError):
                    self._proc.write(data.decode("utf-8", "replace"))
            else:
                self._proc.write(data)
        except Exception as e:
            print(f"[ai_terminal] winpty write error: {e}")

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



# ─── pure terminal core (testable without Sublime) ───────────────────────────
# Screen, Parser, colours, keys, and text layout live in ai/terminal/*.
# This file is the Sublime adapter: ConPTY, view I/O, commands, color-scheme.

try:
    from .terminal.colors import (
        quantize256 as _quantize256,
        pack_attr as _attr,
        xterm256_rgb as _xterm256_rgb,
        XTERM256_RGB as _XTERM256_RGB,
        FG_SHIFT as _FG_SHIFT,
        BG_SHIFT as _BG_SHIFT,
        ATTR_FG_MASK as _ATTR_FG_MASK,
        ATTR_BG_MASK as _ATTR_BG_MASK,
        BOLD as _BOLD,
        REVERSE as _REVERSE,
        FAINT as _FAINT,
        BG_LUMA_THRESHOLD as _BG_LUMA_THRESHOLD,
        ANSI16_HEX as _ANSI16_HEX,
        xterm_hex as _xterm_hex,
        HEX as _HEX,
        scope_name_for as _scope_name_for,
        rstrip_cells as _rstrip_cells,
        scheme_colors_for as _scheme_colors_for,
        ensure_contrast as _ensure_contrast,
        DEFAULT_FG_HEX as _DEFAULT_FG_HEX,
        BG_NEAR_BLACK as _BG_NEAR_BLACK,
        _ANSI16_RGB,
    )
    from .terminal.screen import Screen as _Screen, BLANK as _BLANK
    from .terminal.parser import Parser as _Parser
    from .terminal.keys import (
        KEY_MAP as _KEY_MAP,
        APP_MODE_KEY_MAP as _APP_MODE_KEY_MAP,
        CTRL_KEY_MAP as _CTRL_KEY_MAP,
        ALT_KEY_MAP as _ALT_KEY_MAP,
        SHIFT_KEY_MAP as _SHIFT_KEY_MAP,
        get_key_code as _get_key_code,
        get_ctrl_key_code as _get_ctrl_key_code,
        get_alt_key_code as _get_alt_key_code,
        get_shift_key_code as _get_shift_key_code,
        translate_key as _translate_key,
    )
    from .terminal.pty_env import sanitize_pty_env as _sanitize_pty_env
    from .terminal.render import (
        HOST_CURSOR_SCOPE as _HOST_CURSOR_SCOPE,
        build_text_and_regions as _build_text_and_regions_pure,
        cursor_text_offset as _cursor_text_offset,
        paint_host_cursor as _paint_host_cursor,
        punch_host_cursor_region as _punch_host_cursor_region,
    )
    from .terminal.caret import (
        adjust_display_caret as _adjust_display_caret,
        clear_optimistic_caret as _clear_optimistic_caret,
        note_optimistic_insert as _note_optimistic_insert,
        pad_row_for_caret as _pad_row_for_caret,
    )
    from .terminal.mouse import (
        encode_click as _encode_click,
        encode_mouse as _encode_mouse,
        encode_wheel as _encode_wheel,
        st_button_to_proto as _st_button_to_proto,
        view_point_to_cell as _view_point_to_cell,
    )
except ImportError as _term_imp_err:
    # Unit tests / scripts outside Packages/User use top-level `ai.*`.
    # Do NOT hide a real missing-name error behind "No module named 'ai'".
    try:
        from ai.terminal.colors import (
            quantize256 as _quantize256,
            pack_attr as _attr,
            xterm256_rgb as _xterm256_rgb,
            XTERM256_RGB as _XTERM256_RGB,
            FG_SHIFT as _FG_SHIFT,
            BG_SHIFT as _BG_SHIFT,
            ATTR_FG_MASK as _ATTR_FG_MASK,
            ATTR_BG_MASK as _ATTR_BG_MASK,
            BOLD as _BOLD,
            REVERSE as _REVERSE,
            FAINT as _FAINT,
            BG_LUMA_THRESHOLD as _BG_LUMA_THRESHOLD,
            ANSI16_HEX as _ANSI16_HEX,
            xterm_hex as _xterm_hex,
            HEX as _HEX,
            scope_name_for as _scope_name_for,
            rstrip_cells as _rstrip_cells,
            scheme_colors_for as _scheme_colors_for,
            ensure_contrast as _ensure_contrast,
            DEFAULT_FG_HEX as _DEFAULT_FG_HEX,
            BG_NEAR_BLACK as _BG_NEAR_BLACK,
            _ANSI16_RGB,
        )
        from ai.terminal.screen import Screen as _Screen, BLANK as _BLANK
        from ai.terminal.parser import Parser as _Parser
        from ai.terminal.keys import (
            KEY_MAP as _KEY_MAP,
            APP_MODE_KEY_MAP as _APP_MODE_KEY_MAP,
            CTRL_KEY_MAP as _CTRL_KEY_MAP,
            ALT_KEY_MAP as _ALT_KEY_MAP,
            SHIFT_KEY_MAP as _SHIFT_KEY_MAP,
            get_key_code as _get_key_code,
            get_ctrl_key_code as _get_ctrl_key_code,
            get_alt_key_code as _get_alt_key_code,
            get_shift_key_code as _get_shift_key_code,
            translate_key as _translate_key,
        )
        from ai.terminal.pty_env import sanitize_pty_env as _sanitize_pty_env
        from ai.terminal.render import (
            HOST_CURSOR_SCOPE as _HOST_CURSOR_SCOPE,
            build_text_and_regions as _build_text_and_regions_pure,
            cursor_text_offset as _cursor_text_offset,
            paint_host_cursor as _paint_host_cursor,
            punch_host_cursor_region as _punch_host_cursor_region,
        )
        from ai.terminal.caret import (
            adjust_display_caret as _adjust_display_caret,
            clear_optimistic_caret as _clear_optimistic_caret,
            note_optimistic_insert as _note_optimistic_insert,
            pad_row_for_caret as _pad_row_for_caret,
        )
        from ai.terminal.mouse import (
            encode_click as _encode_click,
            encode_mouse as _encode_mouse,
            encode_wheel as _encode_wheel,
            st_button_to_proto as _st_button_to_proto,
            view_point_to_cell as _view_point_to_cell,
        )
    except ImportError:
        raise _term_imp_err


# ─── colour scheme registration (Sublime-specific) ───────────────────────────
_SCHEME_LOCK = threading.Lock()
_REGISTERED_SCOPES = set()
_SCHEME_PATH = None  # Safely initialized inside _init_dynamic_color_scheme using sublime.packages_path()
# Permanent high-contrast block for host-synthesized cursors (Grok --minimal,
# plain shells). Must not depend on dynamic ai.fb.* registration.
# Foreground must be LIGHT: the host cell is a full-block glyph (█) painted by
# add_regions. ST often applies only the text colour, not the region fill; a
# black foreground then makes █ black-on-black (invisible). White █ reads as a
# solid block even when fill fails.
_HOST_CURSOR_RULE = {
    "scope": "ai.terminal.host_cursor",
    "background": "#CCCCCC",
    "foreground": "#FFFFFF",
}
_BASE_SCHEME = {
    "name": "AI Terminal",
    "variables": {},
    "globals": {
        "background": "#000000",
        "foreground": "#FFFFFF",
        # Host caret must be invisible: Claude/ratatui draw the cursor with
        # reverse fg/bg on a cell. A visible ST caret is a second cursor and
        # looks like an off-by-one (Terminus keeps the host caret hidden).
        "caret": "#000000",
        "selection": "#444444",
        "line_highlight": "#0a0a0a",
        "gutter": "#000000",
        "gutter_foreground": "#808080",
    },
    "rules": [dict(_HOST_CURSOR_RULE)],
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


def _ensure_host_cursor_rule(scheme_data):
    """Guarantee the permanent host-cursor scope exists in scheme rules.

    Returns True if scheme_data was mutated.
    """
    if not isinstance(scheme_data, dict):
        return False
    rules = scheme_data.setdefault("rules", [])
    scope = _HOST_CURSOR_RULE["scope"]
    for r in rules:
        if r.get("scope") == scope:
            # Keep contrast high even if an older rule was muted.
            changed = False
            if r.get("background") != _HOST_CURSOR_RULE["background"]:
                r["background"] = _HOST_CURSOR_RULE["background"]
                changed = True
            if r.get("foreground") != _HOST_CURSOR_RULE["foreground"]:
                r["foreground"] = _HOST_CURSOR_RULE["foreground"]
                changed = True
            return changed
    rules.append(dict(_HOST_CURSOR_RULE))
    return True


def _make_fb_rule(fg, bg):
    """Build one ai.fb.* colour-scheme rule with readable contrast."""
    fh, bh = _scheme_colors_for(fg, bg)
    return {"scope": f"ai.fb.{fg}.{bg}", "background": bh, "foreground": fh}


def _repair_scheme_rules(scheme_data):
    """Fix legacy ai.fb.* rules missing fg or with black-on-black contrast.

    Older dynamic registration wrote background-only rules for default-fg
    scopes (ai.fb.0.*). Sublime's region painter then swaps/drops fg so
    Grok input text on a tinted panel becomes invisible. Also lift any
    near-black-on-near-black pair that survived from ANSI black on dark bg.

    Returns number of rules mutated.
    """
    if not isinstance(scheme_data, dict):
        return 0
    fixed = 0
    rules = scheme_data.setdefault("rules", [])
    for r in rules:
        sc = r.get("scope") or ""
        if not sc.startswith("ai.fb."):
            continue
        parts = sc.split(".")
        if len(parts) != 4:
            continue
        try:
            fg_id, bg_id = int(parts[2]), int(parts[3])
        except ValueError:
            continue
        want_fg, want_bg = _scheme_colors_for(fg_id, bg_id)
        changed = False
        if r.get("background") != want_bg:
            r["background"] = want_bg
            changed = True
        # Always ensure a foreground; repair low-contrast / missing.
        cur_fg = r.get("foreground")
        if not cur_fg:
            r["foreground"] = want_fg
            changed = True
        else:
            fixed_fg = _ensure_contrast(cur_fg, r.get("background") or want_bg)
            if fixed_fg != cur_fg:
                r["foreground"] = fixed_fg
                changed = True
            # Prefer canonical palette fg when we had to invent one from missing.
        if changed:
            fixed += 1
    return fixed


def _init_dynamic_color_scheme():
    global _SCHEME_PATH, _REGISTERED_SCOPES
    try:
        _SCHEME_PATH = os.path.join(sublime.packages_path(), "User", "ai_terminal.sublime-color-scheme")
        if os.path.exists(_SCHEME_PATH):
            size = os.path.getsize(_SCHEME_PATH)
            # If the file size is very large (e.g. the old precompiled 8.9MB static matrix), shrink it to the base scheme.
            # 15MB is a safe threshold to distinguish a dynamic scheme from the old static matrix.
            if size > 15000000:
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
                dirty = False
                # Keep host caret invisible even on schemes written before this rule.
                g = data.setdefault("globals", {})
                bg = g.get("background", "#000000")
                if g.get("caret") != bg:
                    g["caret"] = bg
                    dirty = True
                    _color_scheme_log(f"[init] Forced host caret invisible (matched bg {bg}).")
                if _ensure_host_cursor_rule(data):
                    dirty = True
                    _color_scheme_log("[init] Ensured ai.terminal.host_cursor rule.")
                n_fix = _repair_scheme_rules(data)
                if n_fix:
                    dirty = True
                    _color_scheme_log(
                        f"[init] Repaired {n_fix} ai.fb.* rules "
                        f"(missing fg / low contrast)."
                    )
                if dirty:
                    _save_color_scheme(data)
                _REGISTERED_SCOPES.add(_HOST_CURSOR_RULE["scope"])
            msg = f"[init] Initialized. Loaded {len(_REGISTERED_SCOPES)} registered scope rules from disk ({size} bytes)."
            print(f"[ai_terminal] {msg}")
            _color_scheme_log(msg)
        else:
            _save_color_scheme(_BASE_SCHEME)
            _REGISTERED_SCOPES.add(_HOST_CURSOR_RULE["scope"])
            msg = "[init] Created fresh dynamic color scheme file."
            print(f"[ai_terminal] {msg}")
            _color_scheme_log(msg)
    except Exception as e:
        msg = f"[init] ERROR: Failed to initialize dynamic color scheme: {e}"
        print(f"[ai_terminal] {msg}")
        _color_scheme_log(msg)


def _scheme_disk_paths():
    """All on-disk scheme paths we may read/write (never rely only on _SCHEME_PATH).

    Hot-reload resets module globals so _SCHEME_PATH can be None while the
    User scheme file still exists. Flush used to treat that as 'no file' and
    rewrite BASE+pending only — wiping thousands of rules (peak was 5275).
    """
    paths = []
    if _SCHEME_PATH:
        paths.append(_SCHEME_PATH)
    try:
        user_path = os.path.join(
            sublime.packages_path(), "User", "ai_terminal.sublime-color-scheme"
        )
        if user_path not in paths:
            paths.append(user_path)
    except Exception:
        pass
    # Repo backup copy (SText) when running in a known layout
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        # .../Packages/User/ai/ai_terminal.py -> .../Packages/User/
        user_dir = os.path.dirname(here)
        repo_guess = os.path.join(user_dir, "ai_terminal.sublime-color-scheme")
        if os.path.isfile(repo_guess) and repo_guess not in paths:
            paths.append(repo_guess)
    except Exception:
        pass
    return paths


def _load_scheme_from_disk():
    """Load the largest valid scheme on disk (most rules wins)."""
    best = None
    best_n = -1
    best_path = None
    for p in _scheme_disk_paths():
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            n = len(data.get("rules") or [])
            if n > best_n:
                best, best_n, best_path = data, n, p
        except Exception as e:
            _color_scheme_log(f"[load] ERROR reading {p}: {e}")
    if best is not None:
        _color_scheme_log(f"[load] Using {best_path} with {best_n} rules")
    return best


def _durable_scheme_backup(scheme_data):
    """Keep a dated snapshot under ~/data/logs/ai_terminal/scheme_backups/."""
    try:
        n = len(scheme_data.get("rules") or [])
        if n < 100:
            return
        bdir = os.path.expanduser("~/data/logs/ai_terminal/scheme_backups")
        os.makedirs(bdir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(bdir, f"ai_terminal_{n}rules_{ts}.sublime-color-scheme")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(scheme_data, f, indent=None, separators=(",", ":"))
        # Keep only the 5 newest backups
        bak = sorted(
            (os.path.join(bdir, x) for x in os.listdir(bdir)
             if x.endswith(".sublime-color-scheme")),
            key=os.path.getmtime,
            reverse=True,
        )
        for old in bak[5:]:
            try:
                os.remove(old)
            except Exception:
                pass
        _color_scheme_log(f"[backup] Wrote {path} ({n} rules)")
    except Exception as e:
        _color_scheme_log(f"[backup] ERROR: {e}")


def _save_color_scheme(scheme_data):
    # Never write a scheme that would shrink the on-disk rule set.
    # Absolute floor: never replace a file that has more rules than we're writing
    # when the existing file is "large" (the 5275→2 wipe class of bug).
    try:
        existing = _load_scheme_from_disk()
        if existing is not None:
            old_n = len(existing.get("rules") or [])
            new_n = len(scheme_data.get("rules") or [])
            if old_n > new_n and old_n >= 20:
                msg = (
                    f"[save] REFUSED wipe: disk has {old_n} rules, "
                    f"refusing to write {new_n}"
                )
                print(f"[ai_terminal] {msg}")
                _color_scheme_log(msg)
                return
    except Exception as e:
        _color_scheme_log(f"[save] guard error: {e}")

    paths = _scheme_disk_paths()
    if not paths:
        _color_scheme_log("[save] ERROR: no scheme paths available")
        return

    for p in paths:
        try:
            # Per-path guard: never shrink an individual file
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        prev = json.load(f)
                    prev_n = len(prev.get("rules") or [])
                    new_n = len(scheme_data.get("rules") or [])
                    if prev_n > new_n and prev_n >= 20:
                        _color_scheme_log(
                            f"[save] REFUSED shrink {p}: {prev_n} -> {new_n}"
                        )
                        continue
                except Exception:
                    pass
            os.makedirs(os.path.dirname(p), exist_ok=True)
            temp_path = p + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(scheme_data, f, indent=None, separators=(",", ":"))
            os.replace(temp_path, p)
        except Exception as e:
            print(f"[ai_terminal] Error writing color scheme file to {p}: {e}")
            _color_scheme_log(f"[save] ERROR writing {p}: {e}")

    _durable_scheme_backup(scheme_data)


def _ensure_scopes_hydrated_from_disk():
    """If memory lost scopes after reload, re-read the largest on-disk scheme.

    Without this, register() thinks every scope is new and flush can race with
    a half-initialized path. Call under _SCHEME_LOCK or at init.
    """
    global _REGISTERED_SCOPES
    if len(_REGISTERED_SCOPES) >= 50:
        return
    data = _load_scheme_from_disk()
    if not data:
        return
    n0 = len(_REGISTERED_SCOPES)
    for r in data.get("rules") or []:
        sc = r.get("scope")
        if sc:
            _REGISTERED_SCOPES.add(sc)
    if len(_REGISTERED_SCOPES) > n0:
        _color_scheme_log(
            f"[hydrate] Loaded {len(_REGISTERED_SCOPES) - n0} scopes from disk "
            f"(now {len(_REGISTERED_SCOPES)} in memory)"
        )


def _register_scope_async(fg, bg):
    global _WRITE_PENDING
    scope = f"ai.fb.{fg}.{bg}"
    
    with _SCHEME_LOCK:
        _ensure_scopes_hydrated_from_disk()
        if scope in _REGISTERED_SCOPES:
            return
        _REGISTERED_SCOPES.add(scope)
        _color_scheme_log(
            f"[register] Encountered new scope: {scope} "
            f"(Memory registered count: {len(_REGISTERED_SCOPES)})"
        )
        # Always set fg+bg with minimum contrast (ST bg-only rules hide text).
        _PENDING_RULES.append(_make_fb_rule(fg, bg))
        
        if _WRITE_PENDING:
            return
        _WRITE_PENDING = True
        
    # Throttled / debounced to avoid write storms and ST hot-reload crashes
    sublime.set_timeout_async(_flush_pending_rules, 15000)


def _flush_pending_rules():
    global _WRITE_PENDING, _PENDING_RULES, _SCHEME_PATH
    with _SCHEME_LOCK:
        _WRITE_PENDING = False
        if not _PENDING_RULES:
            return
        rules_to_add = list(_PENDING_RULES)
        _PENDING_RULES.clear()

    # Always re-resolve path (survives importlib.reload clearing globals).
    try:
        _SCHEME_PATH = os.path.join(
            sublime.packages_path(), "User", "ai_terminal.sublime-color-scheme"
        )
    except Exception:
        pass

    scheme_data = _load_scheme_from_disk()

    if not scheme_data:
        # Only create empty base if no scheme file exists anywhere we know.
        any_exists = any(os.path.isfile(p) for p in _scheme_disk_paths())
        if any_exists:
            msg = (
                "[flush] CRITICAL SAFETY: scheme file(s) exist but unreadable; "
                "aborting write to avoid wipe."
            )
            print(f"[ai_terminal] {msg}")
            _color_scheme_log(msg)
            # Put pending rules back so a later flush can retry.
            with _SCHEME_LOCK:
                _PENDING_RULES = rules_to_add + _PENDING_RULES
            return
        scheme_data = dict(_BASE_SCHEME)
        scheme_data["rules"] = []

    # Merge by scope name (dedupe) then append new.
    by_scope = {}
    for r in scheme_data.get("rules") or []:
        sc = r.get("scope")
        if sc:
            by_scope[sc] = r
    for r in rules_to_add:
        sc = r.get("scope")
        if sc:
            by_scope[sc] = r
    scheme_data["rules"] = list(by_scope.values())
    # Keep host caret invisible
    g = scheme_data.setdefault("globals", {})
    bg = g.get("background", "#000000")
    g["caret"] = bg
    _ensure_host_cursor_rule(scheme_data)
    n_fix = _repair_scheme_rules(scheme_data)
    if n_fix:
        _color_scheme_log(f"[flush] Repaired {n_fix} legacy ai.fb.* rules.")

    _save_color_scheme(scheme_data)
    msg = (
        f"[flush] SUCCESS: Flushed {len(rules_to_add)} dynamic rules to disk. "
        f"Total rules: {len(scheme_data.get('rules', []))}"
    )
    print(f"[ai_terminal] {msg}")
    _color_scheme_log(msg)


def _scope_for(attr):
    """Map a packed cell attr to a precompiled scope, or None for default.

    Reverse with default colours must not collapse to (0,0)/None — that made
    Claude's reverse-video block cursor invisible (see terminal.colors).
    """
    if attr == 0:
        return None
    # Prefer the pure helper (keeps reverse-default logic in one place).
    try:
        from .terminal.colors import scope_name_for as _pure_scope
    except ImportError:
        from ai.terminal.colors import scope_name_for as _pure_scope
    scope = _pure_scope(attr)
    if scope is None:
        return None
    # Register dynamic scheme rule if needed (ai.fb.<fg>.<bg>).
    try:
        # "ai.fb.1.16" -> fg=1, bg=16
        parts = scope.split(".")
        fg, bg = int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        return scope
    if scope not in _REGISTERED_SCOPES:
        _register_scope_async(fg, bg)
    return scope


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
_DEFAULT_LAUNCH_COMMAND = ["cmd.exe"]
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


def _refresh_path_env(env):
    """Rebuild Path from HKLM+HKCU registry and merge with the process Path.

    Sublime Text inherits PATH at launch. `setx` / installer PATH edits only
    hit the registry, so a long-lived ST process can miss npm / agent bins.
    Child PTYs get the refreshed Path so menu launches keep working.
    """
    if os.name != "nt" or not isinstance(env, dict):
        return env
    try:
        import winreg
    except ImportError:
        return env

    parts = []
    for root, subkey in (
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(root, subkey) as key:
                raw, typ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        if typ == getattr(winreg, "REG_EXPAND_SZ", 2):
            raw = os.path.expandvars(raw)
        if raw:
            parts.extend(str(raw).split(";"))

    current = (env.get("Path") or env.get("PATH") or "").split(";")
    seen = set()
    merged = []
    for p in parts + current:
        p = (p or "").strip().rstrip("\\")
        if not p:
            continue
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        merged.append(p)
    if not merged:
        return env
    out = dict(env)
    out["Path"] = ";".join(merged)
    out["PATH"] = out["Path"]
    return out


# Windows App Execution Alias: installing WSL puts a 0-byte bash.exe stub on
# PATH ahead of Git Bash. Prefer a real Git install when resolving bare "bash".
_GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)


def _is_wsl_bash_stub(path):
    """True for the WindowsApps WSL bash launcher (not a real shell binary)."""
    if not path:
        return False
    low = os.path.normcase(path)
    if "\\windowsapps\\bash.exe" in low or "/windowsapps/bash.exe" in low:
        return True
    try:
        # App Execution Aliases are often 0-byte reparse points.
        if os.path.isfile(path) and os.path.getsize(path) == 0:
            return "windowsapps" in low
    except OSError:
        pass
    return False


def _prefer_git_bash(resolved, search_path=None):
    """If *resolved* is the WSL bash stub, return Git Bash when installed."""
    if not _is_wsl_bash_stub(resolved):
        return resolved
    for cand in _GIT_BASH_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    # Last resort: any non-stub bash later on PATH (shutil.which only returns first).
    if search_path:
        for entry in search_path.split(os.pathsep):
            cand = os.path.join(entry, "bash.exe")
            if os.path.isfile(cand) and not _is_wsl_bash_stub(cand):
                return cand
    return resolved


def _resolve_launch_argv(argv, env=None):
    """Resolve bare command names for CreateProcessW / winpty.

    CreateProcess only auto-appends ``.exe``. npm global shims are ``.cmd``,
    so bare names like ``opencode`` fail with ERROR_FILE_NOT_FOUND (2) — and
    without use_last_error the plugin used to report GetLastError 0.

    Resolve via ``shutil.which`` (PATHEXT + PATH), then wrap ``.cmd``/``.bat``
    with ``cmd.exe /c`` and ``.ps1`` with PowerShell.

    Bare ``bash`` skips the WSL WindowsApps stub in favour of Git Bash when
    present (WSL is the separate ``WSL Bash`` profile via wsl.exe).
    """
    argv = [str(a) for a in (argv or [])]
    if not argv:
        return argv

    search_path = None
    if env:
        search_path = env.get("Path") or env.get("PATH")

    exe0 = argv[0]
    if os.path.isabs(exe0) and os.path.isfile(exe0):
        resolved = exe0
    else:
        resolved = shutil.which(exe0, path=search_path)
        if not resolved:
            npm = "yes" if search_path and "npm" in search_path.lower() else "no"
            raise FileNotFoundError(
                f"command not found on PATH: {exe0!r} "
                f"(PATH contains npm dir: {npm}). "
                "Fix User PATH or restart Sublime Text after setx/installers."
            )
        if os.path.basename(exe0).lower() in ("bash", "bash.exe"):
            resolved = _prefer_git_bash(resolved, search_path)

    rest = argv[1:]
    low = resolved.lower()
    if low.endswith((".cmd", ".bat")):
        # /d skips AutoRun; list2cmdline will quote paths with spaces.
        return ["cmd.exe", "/d", "/c", resolved, *rest]
    if low.endswith(".ps1"):
        return [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            resolved,
            *rest,
        ]
    return [resolved, *rest]


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

    with _term_lock():
        terms = list(_term_registry().values())
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


# ─── _Terminal: per-view owner + registry ────────────────────────────────────
#
# Registry MUST live on sys, not only as a module global. Hot-reload / re-exec
# of this file (PluginLoader, importlib.reload, or agents replacing
# sys.modules['User.ai.ai_terminal'] with a new module object) would otherwise
# create a second empty dict while the already-registered Command classes still
# close over the first one. New tabs spawn into dict B; keypress looks in dict A
# → silent dead keyboard ("Bash 2 not taking keystrokes").
def _term_registry():
    """Process-global {view_id: _Terminal}. Survives module reload / re-bind."""
    reg = getattr(sys, "_stext_ai_terminals", None)
    if not isinstance(reg, dict):
        reg = {}
        sys._stext_ai_terminals = reg
    return reg


def _term_lock():
    lock = getattr(sys, "_stext_ai_terminals_lock", None)
    if lock is None:
        lock = threading.Lock()
        sys._stext_ai_terminals_lock = lock
    return lock


# Module-level aliases (same objects as on sys after first import).
_TERMINALS = _term_registry()
_REG_LOCK = _term_lock()


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
    def __init__(self, view, pty, screen, parser, spawn_env=None):
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
        self._spawn_env = spawn_env or {}
        # Auto-follow model (Terminus-style): scroll to the bottom to show new
        # Claude output whenever _auto_follow is True. It starts True, flips
        # False when the user scrolls up to read scrollback (detected in the
        # render by vp drifting below the position we last pinned), and
        # re-engages when the user scrolls back near the bottom or types. Fresh
        # per _spawn, so a restart opens at the prompt (bottom) instead of
        # sticking at the top showing the banner.
        self._auto_follow = True
        self._last_vp_y = 0.0
        # Last PTY cell (1-based col,row) hit by a mouse click/drag. Used as
        # the wheel locus — ST's scroll_lines has no pointer coords, and the
        # caret is usually on the command line, which makes TUI scrollbars
        # ignore the wheel. Updated on every routed mouse event.
        self._last_mouse_cell = None
        # Pixel pan accumulator: ST trackpad often only nudges viewport_position
        # (no scroll_lines / no mousemap scroll_up). Clamp converts that dip
        # into TUI wheel ticks before pinning back to (0,0).
        self._vp_pan_accum = 0.0
        self._vp_pan_armed = False  # True after we accept a pan as user scroll
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
        # Geometry watcher for standard terminal resize behavior.
        self._watcher = _LayoutWatcher(self)

    @classmethod
    def from_id(cls, view_id):
        with _term_lock():
            return _term_registry().get(view_id)

    def start(self):
        # Recording patch: asciicast v3. Recording is on if
        # AI_TERMINAL_LOG_LINES is set in the spawn_env setting OR in ST's
        # process environment (_LOG_LINES). Checked per-spawn so a settings
        # edit takes effect on the next Open Ai here... without a restart.
        # When on, open a per-session .cast file (timestamped filename so
        # each session is its own recording -- a resume's replay is a NEW
        # .cast, not appended duplicates) and write the v3 header. Events
        # are appended by _cast() from _on_data / send_string / resize / kill.
        # Recording is on if AI_TERMINAL_LOG_LINES is set in the merged spawn
        # env (profile or legacy top-level) OR in ST's process env.
        log_on = _LOG_LINES
        if not log_on:
            try:
                log_on = bool((self._spawn_env or {}).get("AI_TERMINAL_LOG_LINES"))
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
        # Drop any in-flight mouse hold so a restart doesn't inherit it.
        vid = self.view.id()
        try:
            _mouse_force_release(self, vid)
        except Exception:
            _MOUSE_HOLD.pop(vid, None)
        _MOUSE_LAST_CLICK.pop(vid, None)
        self._last_mouse_cell = None
        self._vp_pan_accum = 0.0
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


# ─── view helpers ─────────────────────────────────────────────────────────────

_VIEW_NAME = "Ai"
_VIEW_SETTING = "ai_terminal_view"
_TAG_SETTING = "ai_logger"  # so panic_dialog / ClaudeSendTab still find this view


def _vwrite(view, text):
    def _do(t=text):
        view.set_read_only(False)
        view.run_command("append", {"characters": t, "scroll_to_end": True})
    sublime.set_timeout(_do, 0)


class _LayoutWatcher:
    """Standard terminal-style layout watcher.

    Watches a single ai_terminal view for genuine geometry changes (window
    resize, gutter/line_numbers/fold_buttons/margin toggles, font changes)
    and resizes the PTY only when the measured (cols, rows) actually changes.

    Key design choices that prevent the resize<->replay oscillation bug:
      - We never auto-resize in response to PTY output.
      - We measure only the viewport; transient content-width fluctuations
        (scrollbars appearing/disappearing because of the TUI's own output)
        do not change the viewport size, so they do not trigger a resize.
      - Changes are debounced: rapid-fire layout events coalesce into one
        resize call after the viewport size has been stable for a short window.
      - We resize only if the new (cols, rows) differs from the last one we
        told the PTY.
    """

    _DEBOUNCE_MS = 150
    _POLL_MS = 250

    def __init__(self, term):
        self.term = term
        self._pending = False
        self._token = None
        self._last_measure = None

    def request(self):
        """Request a resize check. Safe to call frequently; debounces."""
        if self._pending:
            return
        self._pending = True
        self._token = sublime.set_timeout(lambda: self._run(), self._DEBOUNCE_MS)

    def _run(self):
        self._pending = False
        view = self.term.view
        if not view or not view.is_valid():
            return
        try:
            size = _measure(view)
        except Exception as e:
            print(f"[ai_terminal] layout measure error: {e}")
            return
        if size == self._last_measure:
            return
        self._last_measure = size
        cols, rows = size
        if (cols, rows) != (self.term._last_cols, self.term._last_rows):
            self.term.resize(cols, rows)
            print(f"[ai_terminal] resized PTY to {cols}x{rows}")

    def dispose(self):
        if self._token is not None:
            try:
                sublime.cancel_timeout(self._token)
            except Exception:
                pass
            self._token = None
        self._pending = False


# Lightweight periodic watcher: real window resizes and some setting toggles do
# not always fire add_on_change, so we poll the measured size every 250ms and
# resize only when it actually changes. This is NOT auto-resize-on-output: it
# is purely geometry polling.
_WATCHER_TOKEN = None


def _start_layout_watcher():
    global _WATCHER_TOKEN
    if _WATCHER_TOKEN is not None:
        return
    _layout_tick()


def _layout_tick():
    global _WATCHER_TOKEN
    _WATCHER_TOKEN = None
    try:
        with _term_lock():
            terms = list(_term_registry().values())
        for term in terms:
            if term._watcher is None:
                continue
            term._watcher.request()
    except Exception as e:
        print(f"[ai_terminal] layout watcher error: {e}")
    _WATCHER_TOKEN = sublime.set_timeout(_layout_tick, _LayoutWatcher._POLL_MS)


def _stop_layout_watcher():
    global _WATCHER_TOKEN
    if _WATCHER_TOKEN is not None:
        try:
            sublime.cancel_timeout(_WATCHER_TOKEN)
        except Exception:
            pass
        _WATCHER_TOKEN = None


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
    # Host caret OFF — the TUI paints the cursor via reverse video (SGR 7).
    # Never enable block_caret here; a visible ST caret doubles the TUI cursor
    # and reads as wrong position / off-by-one. Terminus does the same.
    v.settings().set("block_caret", False)
    v.settings().set("caret_extra_width", 0)
    try:
        ts = sublime.load_settings(_SETTINGS_NAME)
        font = ts.get("terminal_font")
        if font:
            v.settings().set("font_face", font)
    except Exception:
        pass
    # draw_centered=False isolates the terminal from the user's global
    # preference. scroll_past_end must be True so two-finger trackpad gestures
    # keep generating scroll_lines even when the TUI framebuffer fits the
    # viewport — with False, ST fires scroll once then stops ("worked once,
    # then dead"). Visible bounce is prevented by always swallowing
    # scroll_lines and pinning the viewport, not by disabling scroll_past_end.
    # NOTE: is_widget=True was tried (matching Terminus) to stop ST's
    # on-activate viewport reposition, but it makes ST hide the main menu while
    # the terminal is focused -- unacceptable, so it is NOT set.
    v.settings().set("draw_centered", False)
    v.settings().set("scroll_past_end", True)
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
        # Genuine windowing-layer trigger (gutter/line_numbers/fold_buttons/
        # margin toggle) -- safe to resize. The poller's auto-resize path
        # (re-measuring viewport_extent after PTY output) is the feedback loop
        # and is disabled; this is NOT the poller.
        # sublime.set_timeout(lambda: _trigger_resize_for(vid), 0)
        pass

    for _key in ("gutter", "line_numbers", "fold_buttons", "margin", "font_face", "font_size"):
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
    # min_columns / max_columns). Subtract 3.5 char widths to account
    # for ST's scroll range math plus fractional em_width precision: the -3
    # is needed for the +1 end-of-line caret and ~2 chars of ST padding.
    # The +0.5 buffer prevents horizontal scrollbars from appearing and
    # re-triggering the layout callbacks within the debounce window.
    mn, mx = _cols_bounds()
    cols = max(mn, int((usable_w - 4.0 * cw) / cw))
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

# Throttle full paints. 40ms was fine for single keys; under burst typing each
# frame does view.replace + add_regions on the whole TUI and starves ST's
# key dispatch on Windows. Keep ~40ms for snappy single keys, but never paint
# faster than MIN_INTERVAL during a continuous stream.
_RENDER_MS = 40
_RENDER_MIN_INTERVAL_MS = 55


def _schedule_render(term):
    if term._render_pending:
        # Mark that more work arrived while armed; _do_render re-arms if dirty.
        term._render_coalesce = True
        return
    term._render_pending = True
    term._render_coalesce = False
    delay = _RENDER_MS
    last = getattr(term, "_last_render_mono", 0.0) or 0.0
    if last:
        # Cap paint rate so burst typing cannot schedule a full replace every
        # key once the previous frame completes.
        elapsed_ms = (time.monotonic() - last) * 1000.0
        if elapsed_ms < _RENDER_MIN_INTERVAL_MS:
            delay = max(delay, int(_RENDER_MIN_INTERVAL_MS - elapsed_ms))
    sublime.set_timeout(lambda: _do_render(term), delay)


def _plain_cells_signature(rows):
    """Stable content fingerprint before host-cursor paint (no ST deps)."""
    if not rows:
        return ""
    # Join chars only — attrs changes still need a full colour rebuild, so
    # include a coarse attr token per cell. Cheap string, not a hash object.
    parts = []
    for row in rows:
        for ch, attr in row:
            parts.append(ch)
            if attr:
                parts.append("\x00")
                parts.append(str(int(attr)))
        parts.append("\n")
    return "".join(parts)


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
    # Host cursor: ST caret stays invisible when the app paints reverse-video
    # (Claude). When it does not (Grok / shells), paint_host_cursor puts a
    # white █ on the blank insertion cell (display-only). Caret row must come
    # from adjust_display_caret — Grok's live `│ >` row, not a history `>`.

    with term._lock:
        rows, cy, cx = term.screen.render_cells()
        cy, cx = _adjust_display_caret(term.screen, cy, cx)
        rows = _pad_row_for_caret(rows, cy, cx)
        # Clear under the lock so a concurrent parser feed cannot set dirty
        # then have us wipe it without painting that feed.
        term.screen.dirty = False
    plain_sig = _plain_cells_signature(rows)
    rows, _host_painted = _paint_host_cursor(rows, cy, cx)
    text, regions = _build_text_and_regions(rows)
    caret_off = _cursor_text_offset(rows, cy, cx)
    # Host-only pads above+below: trackpad can pan both ways. Shift colour
    # region offsets and caret by the top pad length (newlines only).
    top_pad_chars = _HOST_SCROLL_PAD_LINES  # "\n" * N → N chars
    if top_pad_chars and regions:
        regions = [
            (b + top_pad_chars, e + top_pad_chars, scope)
            for (b, e, scope) in regions
        ]
    if caret_off is not None:
        caret_off = caret_off + top_pad_chars
    text = _append_host_scroll_pad(text)
    # Drop any leftover HTML host-cursor phantoms from the prior experiment.
    _clear_host_cursor_phantom(view)

    prev_plain = getattr(term, "_last_plain_sig", None)
    prev_text = getattr(term, "_last_render_text", None)
    prev_caret = getattr(term, "_last_caret_off", None)
    # Burst typing / optimistic caret: screen content often unchanged between
    # keys; only the host █ + ST selection move. Skip full colour rebuild and
    # prefer small patches so the main thread can keep taking keypresses.
    fast_caret = (
        prev_plain is not None
        and prev_plain == plain_sig
        and prev_text is not None
        and prev_text != text
    )
    skip_all = (
        prev_text is not None
        and prev_text == text
        and prev_caret == (caret_off if caret_off is not None else -1)
    )
    if skip_all:
        term._last_render_mono = time.monotonic()
        if term.screen.dirty or getattr(term, "_render_coalesce", False):
            term._render_coalesce = False
            if term.screen.dirty:
                _schedule_render(term)
        return

    # Absolute caret offset (with top pad) so mid-line typing stays put.
    # Do not pass prev_text through command args (ST JSON-serializes them;
    # duplicating a full TUI buffer per key would reintroduce main-thread lag).
    # AiTerminalRenderCommand diffs against the live view when fast_caret.
    view.run_command(
        "ai_terminal_render",
        {
            "text": text,
            "cursor": [cy, cx],
            "cursor_offset": caret_off if caret_off is not None else -1,
            "regions": regions,
            "fast_caret": bool(fast_caret),
        },
    )
    term._last_plain_sig = plain_sig
    term._last_render_text = text
    term._last_caret_off = caret_off if caret_off is not None else -1
    term._last_render_mono = time.monotonic()
    # If keys/PTY dirtied the screen while we painted, arm another frame.
    if term.screen.dirty or getattr(term, "_render_coalesce", False):
        term._render_coalesce = False
        if term.screen.dirty:
            _schedule_render(term)


def _build_text_and_regions(rows):
    """Flatten structured rows into the view text + colour regions.

    Delegates layout to the pure core; uses _scope_for so new scopes still
    get registered into the dynamic color scheme.
    """
    return _build_text_and_regions_pure(rows, scope_for=_scope_for)



# Per-view set of colour region keys added last frame, so we can erase stale
# scopes (whose cells scrolled away or changed attr) on the next render.
_LAST_COLOR_KEYS = {}
_COLOR_KEY_PREFIX = "ai_term_c_"
# Legacy key from the HTML-phantom experiment; still erased so old sessions
# do not keep an inserted grey cell after upgrade.
_HOST_CURSOR_PHANTOM = "ai_term_host_cursor"


def _clear_host_cursor_phantom(view):
    try:
        view.erase_phantoms(_HOST_CURSOR_PHANTOM)
    except Exception:
        pass


def _apply_color_regions(view, regs):
    """Group regions by scope and add them; erase any scope keys we added last
    frame but did not re-add this frame, so stale colour doesn't linger.

    Host cursor is applied last so its fill wins ST's undefined region z-order.
    """
    by_scope = {}
    host_rs = []
    for begin, end, scope in regs:
        if scope == _HOST_CURSOR_SCOPE:
            host_rs.append(sublime.Region(begin, end))
            continue
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
    # Permanent grey block last (z-order). Same flags as colour runs.
    if host_rs:
        key = _COLOR_KEY_PREFIX + _HOST_CURSOR_SCOPE
        view.add_regions(
            key, host_rs, scope=_HOST_CURSOR_SCOPE, flags=sublime.DRAW_NO_OUTLINE
        )
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
            # Fallback if arrows aren't bound to ai_terminal_keypress.
            # No scroll_to_bottom (resize thrash with layout watcher).
            if by == "characters":
                term.send_string("\x1b[C" if fwd else "\x1b[D")
                return ("ai_terminal_noop", {})
            if by == "lines":
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
        if term._watcher is not None:
            term._watcher.dispose()
            term._watcher = None
        with _term_lock():
            _term_registry().pop(self.view.id(), None)
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


def _mouse_hist_len(term):
    """Lines above the PTY grid in the ST view (host top pad + scrollback).

    Host top pad is always present; terminal history is only on main screen.
    """
    pad = _HOST_SCROLL_PAD_LINES
    if term.screen.alt_screen:
        return pad
    return pad + len(term.screen.history)


def _event_to_pty_cell(view, term, event):
    """Map a ST mouse event to 1-based (col, row) in the PTY grid, or None."""
    if not event or "x" not in event or "y" not in event:
        return None
    try:
        pt = view.window_to_text((event["x"], event["y"]))
        row, col = view.rowcol(pt)
    except Exception:
        return None
    return _view_point_to_cell(
        row,
        col,
        hist_len=_mouse_hist_len(term),
        screen_rows=term.screen.rows,
        screen_cols=term.screen.cols,
    )


# Button-hold state for 1002/1003:
#   view_id -> (proto_btn, col, row, gen, t0, moved)
# ST never delivers a clean mouse-up. We release after idle so:
#   tap   = press … short idle → release  (touchpad-friendly)
#   drag  = press … motion… longer idle → release
#   double = 2nd hit same cell / ST by=words → release + full click
_MOUSE_HOLD = {}
# Last completed click (for double-tap after release): view_id -> (col, row, t)
_MOUSE_LAST_CLICK = {}
# Touchpad taps are one short drag_select; complete them quickly.
_MOUSE_TAP_RELEASE_MS = 130
# After the pointer actually moves (thumb drag), keep hold longer.
_MOUSE_DRAG_RELEASE_MS = 500
# Touchpads are slower than mice; allow a wider double-tap window.
_MOUSE_DBLCLICK_MS = 700
# Rows at the bottom of a fullscreen TUI are usually the input line — bad
# default for two-finger scroll (would aim at the prompt, not the history).
_WHEEL_AVOID_BOTTOM_ROWS = 4


def _mouse_force_release(term, view_id):
    """Emit SGR/X10 release for any held button and clear hold state."""
    hold = _MOUSE_HOLD.pop(view_id, None)
    if not hold or term is None:
        return
    btn, col, row = hold[0], hold[1], hold[2]
    try:
        sgr = term.screen.mouse_sgr
        term.send_string(
            _encode_mouse(btn, col, row, press=False, sgr=sgr)
        )
        _MOUSE_LAST_CLICK[view_id] = (col, row, time.time())
    except Exception:
        pass


def _schedule_mouse_release(view, gen, delay_ms):
    """Release the held button if no further drag events arrive."""
    vid = view.id()

    def _fire():
        hold = _MOUSE_HOLD.get(vid)
        if hold is None or hold[3] != gen:
            return
        t = _Terminal.from_id(vid)
        _mouse_force_release(t, vid)

    sublime.set_timeout(_fire, int(delay_ms))


def _send_full_click(term, view_id, proto, col, row, sgr):
    """Press+release one click and remember it for double-tap detection."""
    _mouse_force_release(term, view_id)
    term.send_string(_encode_click(proto, col, row, sgr=sgr))
    _MOUSE_LAST_CLICK[view_id] = (col, row, time.time())


def _wheel_locus(view, term):
    """1-based (col, row) for wheel reports when ST gives no pointer.

    Priority:
      1) last click/drag cell (user was aiming at scrollbar / history)
      2) centre of the history panel (not the command line, not only the
         far-right chrome — Grok often needs the pointer over the message list)
    """
    cell = getattr(term, "_last_mouse_cell", None)
    if cell is not None:
        return cell
    cols = max(1, int(term.screen.cols))
    rows = max(1, int(term.screen.rows))
    avoid = min(_WHEEL_AVOID_BOTTOM_ROWS, max(0, rows - 1))
    usable = max(1, rows - avoid)
    row = max(1, (usable + 1) // 2)
    col = max(1, (cols + 1) // 2)  # message list centre
    return col, row


def _route_mouse_click(view, term, event, *, discrete_click=False):
    """Send a mouse report when the app enabled tracking. Return True if handled.

    discrete_click: True for multi-click (ST by=words/lines) — always a full
    press+release so double-clicks reach the TUI instead of becoming ST word
    selection. False for normal drag_select: in 1002/1003 modes use press +
    motion + idle release so scroll-thumb drag works; in 1000 mode always
    press+release.

    Multi-click often arrives without event x/y — fall back to last cell so
    double-clicks on the scroll control still reach the PTY.

    Touchpad notes: taps are short; we auto-release quickly until motion is
    seen, then keep the hold longer for drag-grab. Double-tap window is wide.
    """
    mode = term.screen.mouse_tracking
    if not mode:
        return False
    cell = _event_to_pty_cell(view, term, event)
    if cell is None and discrete_click:
        cell = getattr(term, "_last_mouse_cell", None)
    if cell is None:
        # Click in scrollback (or off-grid): let ST select text.
        return False
    st_btn = event.get("button", 1) if event else 1
    proto = _st_button_to_proto(st_btn)
    if proto is None:
        return False
    col, row = cell
    term._last_mouse_cell = (col, row)
    # User is interacting with the TUI chrome — don't yank viewport to bottom.
    term._auto_follow = False
    sgr = term.screen.mouse_sgr
    vid = view.id()
    now = time.time()

    # Multi-click or click-only mode: complete any prior hold, then one click.
    if discrete_click or mode < 1002:
        _send_full_click(term, vid, proto, col, row, sgr)
        return True

    # 1002 (drag) / 1003 (any-event): press, then motion while held.
    hold = _MOUSE_HOLD.get(vid)
    if hold is None:
        # Fresh press. If a previous click just finished on this cell, this is
        # the second half of a double-tap (common on touchpads after release).
        prev = _MOUSE_LAST_CLICK.get(vid)
        if (
            prev is not None
            and prev[0] == col
            and prev[1] == row
            and (now - prev[2]) * 1000.0 <= _MOUSE_DBLCLICK_MS
        ):
            _send_full_click(term, vid, proto, col, row, sgr)
            return True
        seq = _encode_mouse(proto, col, row, press=True, sgr=sgr)
        gen = 1
        _MOUSE_HOLD[vid] = (proto, col, row, gen, now, False)
        term.send_string(seq)
        _schedule_mouse_release(view, gen, _MOUSE_TAP_RELEASE_MS)
        return True

    btn_prev, c_prev, r_prev, gen_prev, t0, moved = hold
    elapsed_ms = (now - t0) * 1000.0
    same_cell = (col, row) == (c_prev, r_prev)

    # Second hit on the same cell soon after press, without having dragged =
    # double-click (ST sometimes omits by=words). Finish first click, send
    # a full second click. Do NOT emit "motion" — that ate double-clicks.
    if same_cell and not moved and elapsed_ms <= _MOUSE_DBLCLICK_MS:
        _send_full_click(term, vid, proto, col, row, sgr)
        return True

    # Same cell, still holding, no movement: ST re-delivery / jitter. Keep
    # the press alive (refresh idle timer) without spamming the TUI.
    if same_cell:
        gen = gen_prev + 1
        _MOUSE_HOLD[vid] = (proto, col, row, gen, t0, moved)
        delay = _MOUSE_DRAG_RELEASE_MS if moved else _MOUSE_TAP_RELEASE_MS
        _schedule_mouse_release(view, gen, delay)
        return True

    # Cell changed → drag motion (scroll-thumb grab).
    seq = _encode_mouse(proto, col, row, press=True, motion=True, sgr=sgr)
    gen = gen_prev + 1
    _MOUSE_HOLD[vid] = (proto, col, row, gen, t0, True)
    term.send_string(seq)
    _schedule_mouse_release(view, gen, _MOUSE_DRAG_RELEASE_MS)
    return True


def _scroll_tick_count(amount):
    """How many fine scroll steps for one gesture (feel, not max throughput).

    Keep this low: stacking PageUp×N + arrows×N + dual wheel felt jumpy even
    when the *direction* was correct.
    """
    try:
        a = abs(float(amount))
    except (TypeError, ValueError):
        a = 1.0
    if a <= 0:
        return 1
    if a < 1.0:
        return 1
    if a < 2.5:
        return 2
    return min(3, int(round(a)))


def _route_mouse_wheel(view, term, amount):
    """Forward trackpad scroll to the PTY using *content-grab* semantics.

    The user drags the *text* (like grabbing the buffer), not the scroll
    thumb. That is the opposite of the TUI scroll-button, which moves the
    *view*:

      amount > 0 → text dragged downward on screen → reveal older (above)
      amount < 0 → text dragged upward on screen   → reveal newer (below)

    Internally we still emit Page/arrow/wheel "up" for older and "down" for
    newer; only the sign of `amount` vs finger motion is content-grab.

    Feel: fine steps (wheel + arrows); one Page only on a fling.
    """
    try:
        # amount > 0 → older history (keys/wheel "up")
        see_older = float(amount) > 0
    except (TypeError, ValueError):
        see_older = True
    n = _scroll_tick_count(amount)
    term._auto_follow = False
    term._last_scroll_send_t = time.time()

    parts = []
    try:
        arrow = _get_key_code("up" if see_older else "down")
    except Exception:
        arrow = "\x1b[A" if see_older else "\x1b[B"
    parts.append(arrow * n)
    if term.screen.mouse_tracking:
        col, row = _wheel_locus(view, term)
        sgr = term.screen.mouse_sgr
        parts.append(
            "".join(
                _encode_wheel(see_older, col, row, sgr=sgr) for _ in range(n)
            )
        )
    if n >= 3:
        try:
            page = _get_key_code("pageup" if see_older else "pagedown")
        except Exception:
            page = "\x1b[5~" if see_older else "\x1b[6~"
        parts.append(page)
    term.send_string("".join(parts))
    return True


def _pin_terminal_viewport(view, term):
    """Snap viewport back after a trackpad pan (kill visible jiggle).

    TUI / mouse-tracking: rest at top of real content (below top pad) so
    both up and down pans still have headroom. Not y=0 — that blocked
    down-drag forever.
    """
    try:
        view.settings().set("scroll_past_end", True)
        if term is not None and (
            term.screen.alt_screen or term.screen.mouse_tracking
        ):
            rest = _host_rest_y(view)
            view.set_viewport_position((0.0, rest), False)
            return
        le = view.layout_extent()
        ve = view.viewport_extent()
        lh = view.line_height() or 12.0
        # Near-fit including pads: still use rest position
        if le[1] - ve[1] <= lh * (2 * _HOST_SCROLL_PAD_LINES + 1):
            view.set_viewport_position((0.0, _host_rest_y(view)), False)
        elif term is not None and getattr(term, "_auto_follow", False):
            _scroll_to_bottom(view)
    except Exception:
        pass


class AiTerminalKeyInterceptor(sublime_plugin.EventListener):
    """Ctrl+C/V, and mouse → PTY when the app enabled DEC mouse tracking."""

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
        # ── Xterm mouse tracking (Grok / fullscreen TUIs) ──────────────────
        # Apps enable via CSI ?1000/1002/1003 h (+ usually ?1006 h SGR).
        # Without this, clicks only move ST's selection and never reach the PTY.
        # Shift+click / extend keep ST selection (xterm shift-bypass). Multi-click
        # (double-click → by=words) is FORWARD when tracking is on — dropping it
        # made TUI scroll buttons ignore double-clicks and left ST selecting
        # "words" inside the framebuffer instead.
        if command_name == "drag_select":
            args = args or {}
            event = args.get("event") or {}
            if args.get("extend") or args.get("additive") or args.get("subtractive"):
                return None
            multi = args.get("by") in ("words", "lines", "columns")
            if _route_mouse_click(view, term, event, discrete_click=multi):
                return ("ai_terminal_noop", {})
            return None
        # Two-finger trackpad / mouse wheel: ALWAYS swallow for terminal views.
        # Returning None lets ST pan the view a few px; our clamp loop then
        # snaps it back — the "tab jumps then restores" glitch.
        # Always forward vertical scroll to the PTY (wheel if mouse tracking,
        # else PageUp/Down) and pin the viewport so the tab never visibly pans.
        if command_name in ("scroll_lines", "scroll_horizontally"):
            args = args or {}
            if command_name == "scroll_lines":
                amt = args.get("amount", 1)
                n = int(getattr(term, "_scroll_lines_log_n", 0) or 0)
                if n < 12:
                    print(
                        f"[ai_terminal] scroll_lines amount={amt!r} "
                        f"mouse={term.screen.mouse_tracking} "
                        f"alt={term.screen.alt_screen}"
                    )
                    term._scroll_lines_log_n = n + 1
                _route_mouse_wheel(view, term, amt)
            _pin_terminal_viewport(view, term)
            # Immediate second pin on next tick: ST may apply residual pan
            # after on_text_command returns even when we replace the command.
            sublime.set_timeout(
                lambda v=view, t=term: _pin_terminal_viewport(v, t), 0
            )
            return ("ai_terminal_noop", {})
        return None


# ─── commands ────────────────────────────────────────────────────────────────


# Markers that mean "this directory is a project root" (Claude / agents care).
# Checked after `.git`; first hit walking *up* from a file wins (nearest root).
_PROJECT_MARKERS = (
    "CLAUDE.md",
    "Claude.md",
    "AGENTS.md",
    "Agents.md",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "composer.json",
)


def _norm_path(path):
    return os.path.normcase(os.path.abspath(path))


def _containing_window_folder(window, path):
    """Deepest window.folders() entry that contains path, or None."""
    if not window or not path:
        return None
    path_n = _norm_path(path)
    best = None
    best_len = -1
    for folder in window.folders() or []:
        folder_n = _norm_path(folder)
        if path_n == folder_n or path_n.startswith(folder_n + os.sep):
            if len(folder_n) > best_len:
                best = folder
                best_len = len(folder_n)
    return best


def _has_git(path):
    """True if path is a git work tree (dir) or gitfile (submodule)."""
    if not path or not os.path.isdir(path):
        return False
    git = os.path.join(path, ".git")
    return os.path.isdir(git) or os.path.isfile(git)


def _has_project_markers(path):
    if not path or not os.path.isdir(path):
        return False
    return any(os.path.exists(os.path.join(path, m)) for m in _PROJECT_MARKERS)


def _looks_like_project_root(path):
    return _has_git(path) or _has_project_markers(path)


def _nearest_project_root(path, stop_at=None):
    """Walk up from path; return nearest project root for agent cwd.

    Prefers a `.git` directory (real repo) over markdown/package markers so an
    umbrella folder like ~/projects that only has CLAUDE.md/AGENTS.md does not
    win over nested repos (SText, finance, …). Stops at *stop_at* (inclusive)
    when given — typically the containing window folder.
    """
    if not path:
        return None
    cur = os.path.abspath(path if os.path.isdir(path) else os.path.dirname(path))
    if not cur:
        return None
    stop = os.path.abspath(stop_at) if stop_at else None
    stop_n = _norm_path(stop) if stop else None
    start = cur
    marker_hit = None

    while True:
        if _has_git(cur):
            return cur
        if marker_hit is None and _has_project_markers(cur):
            marker_hit = cur
        at_stop = bool(stop_n and _norm_path(cur) == stop_n)
        parent = os.path.dirname(cur)
        if at_stop or parent == cur:
            return marker_hit or (stop if at_stop else None) or start
        if stop_n:
            parent_n = _norm_path(parent)
            # Do not walk above the window-folder boundary.
            if not (parent_n == stop_n or stop_n.startswith(parent_n + os.sep)
                    or parent_n.startswith(stop_n + os.sep)):
                return marker_hit or stop or start
            if len(parent_n) < len(stop_n) and not stop_n.startswith(parent_n + os.sep):
                return marker_hit or stop or start
        cur = parent


def _child_project_dirs(folder, limit=80):
    """Immediate subdirs worth offering as agent cwd.

    Prefers git-backed children. Marker-only dirs (CLAUDE.md / AGENTS.md with
    no .git) are used only when the parent has no git children — so an umbrella
    like ~/projects is not re-listed under ~ just because it has a layer-1 map.
    """
    git_kids = []
    marker_kids = []
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        return []
    for name in names:
        if name.startswith("."):
            continue
        child = os.path.join(folder, name)
        if not os.path.isdir(child):
            continue
        if _has_git(child):
            git_kids.append(child)
        elif _has_project_markers(child):
            marker_kids.append(child)
    chosen = git_kids if git_kids else marker_kids
    return chosen[:limit]


def _cwd_candidates(window):
    """Folders the user can sensibly launch an agent into.

    Umbrella sidebar roots (e.g. ~/projects with many nested repos) expand to
    those children. The umbrella itself is only listed when it is a real git
    repo (monorepo) — a CLAUDE.md at the projects map layer is not enough.
    """
    folders = list(window.folders() or []) if window else []
    if not folders:
        return []
    candidates = []
    seen = set()

    def _add(p):
        n = _norm_path(p)
        if n in seen:
            return
        seen.add(n)
        candidates.append(p)

    for folder in folders:
        children = _child_project_dirs(folder)
        if children:
            for child in children:
                _add(child)
            # Monorepo root: also offer the folder itself.
            if _has_git(folder):
                _add(folder)
        else:
            _add(folder)
    return candidates


def _sole_auto_cwd(folders):
    """Return a single unambiguous cwd, or None to force a picker.

    A lone window folder is only auto-used when it is a real git repo, or when
    it has no nested project children. Umbrella maps (CLAUDE.md + many repos)
    always return None so the user picks the real project.
    """
    if len(folders) != 1:
        return None
    folder = folders[0]
    if _child_project_dirs(folder):
        return folder if _has_git(folder) else None
    return folder


def _resolve_editor_path(view):
    """cwd for editor/context launches: nearest project root for the file."""
    window = view.window()
    path = view.file_name()
    if path:
        boundary = _containing_window_folder(window, path)
        return _nearest_project_root(path, stop_at=boundary)
    folders = window.folders() if window else []
    return _sole_auto_cwd(folders or [])


def _resolve_here_path(window, paths):
    """cwd for sidebar / Tools-menu launches.

    Priority:
      1. Explicit sidebar paths (dir as-is; file -> its project root)
      2. Active view's nearest project root
      3. Sole unambiguous window folder
      4. None → caller should offer a picker (never silently use umbrella dirs)
    """
    if paths:
        path = paths[0]
        if os.path.isdir(path):
            return path
        boundary = _containing_window_folder(window, path)
        return _nearest_project_root(path, stop_at=boundary)

    view = window.active_view() if window else None
    if view and view.file_name():
        boundary = _containing_window_folder(window, view.file_name())
        return _nearest_project_root(view.file_name(), stop_at=boundary)

    folders = window.folders() if window else []
    return _sole_auto_cwd(folders or [])


def _pick_cwd_then(window, on_path):
    """Resolve cwd or show a quick panel of project candidates, then call on_path."""
    path = _resolve_here_path(window, [])
    if path:
        on_path(path)
        return

    candidates = _cwd_candidates(window)
    if not candidates:
        sublime.status_message("Ai terminal: no folder resolved")
        return
    if len(candidates) == 1:
        on_path(candidates[0])
        return

    labels = []
    for c in candidates:
        base = os.path.basename(c.rstrip("\\/")) or c
        labels.append([base, c])

    def on_done(idx):
        if idx < 0:
            return
        on_path(candidates[idx])

    window.show_quick_panel(labels, on_done)


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
    
    # Host (ST plugin host / agent shells) often has NO_COLOR=1, FORCE_COLOR=0,
    # TERM=dumb — Grok doctor then reports color=none. Sanitize before spawn;
    # profile spawn_env still wins for any key it sets.
    env = _sanitize_pty_env(os.environ, extra_env)
    # Pick up PATH changes from setx/installers without requiring an ST restart.
    env = _refresh_path_env(env)

    try:
        argv = _resolve_launch_argv(argv, env)
    except FileNotFoundError as e:
        sublime.error_message(f"ai_terminal: {e}")
        view.close()
        return

    print(f"[ai_terminal] launch cwd: {path!r}")
    print(f"[ai_terminal] launch argv: {argv!r}")

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
    screen = _Screen(cols, rows, history_cap=_scrollback_size())
    parser = _Parser(screen, force_main_screen=_force_main_screen())
    term = _Terminal(view, pty, screen, parser, spawn_env=extra_env)
    with _term_lock():
        _term_registry()[view.id()] = term
    term.start()


class AiTerminalOpenHereCommand(sublime_plugin.WindowCommand):
    """Open a Claude TUI terminal in the chosen directory.

    Resolves cwd from sidebar selection, else the active file's nearest
    project root (.git / CLAUDE.md / …), else a quick-panel of project
    folders — never silently falls back to an umbrella sidebar root
    like ~/projects.

    Menu: Side Bar.sublime-menu — "Open Ai Terminal here..."
          Main.sublime-menu → Tools → Ai Terminal → profiles
    Command palette: "Ai: Open Terminal Here"
    """

    def run(self, paths=None, profile=None):
        paths = paths or []
        if paths:
            path = _resolve_here_path(self.window, paths)
            if not path:
                sublime.status_message("Ai terminal: no folder resolved")
                return
            _spawn(self.window, path, profile=profile)
            return

        def on_path(path):
            _spawn(self.window, path, profile=profile)

        _pick_cwd_then(self.window, on_path)

    def is_visible(self, paths=None):
        return True


class AiTerminalOpenInEditorCommand(sublime_plugin.TextCommand):
    """Open a Claude TUI terminal in the active file's project root.

    Uses the nearest project root containing the file (not the bare
    file directory, and not the umbrella window folder).

    Menu: Context.sublime-menu / Tab Context.sublime-menu — "Open Ai Terminal here..."
    Command palette: "Ai: Open Terminal in Editor"
    """

    def run(self, edit, profile=None):
        window = self.view.window()
        path = _resolve_editor_path(self.view)
        if path and window:
            _spawn(window, path, profile=profile)
            return
        if not window:
            sublime.status_message("Ai terminal: no folder resolved")
            return

        def on_path(p):
            _spawn(window, p, profile=profile)

        _pick_cwd_then(window, on_path)


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


class AiTerminalSendStringWindowCommand(sublime_plugin.WindowCommand):
    """Window-level variant: send a string to the terminal PTY without needing
    the terminal view to be focused.

    Resolves the target terminal view in this order:
      1. the active view in the window, if it is an ai_terminal view;
      2. otherwise the first ai_terminal view found in the window.

    Lets external callers (agents, other plugins, key bindings scoped to a
    non-terminal context) inject input into the terminal from anywhere.

    No key/menu/palette binding; invoked programmatically.
    """

    def run(self, string=""):
        view = self.window.active_view()
        if view is None or not view.settings().get(_VIEW_SETTING, False):
            for v in self.window.views():
                if v.settings().get(_VIEW_SETTING, False):
                    view = v
                    break
        if view is None:
            return
        term = _Terminal.from_id(view.id())
        if term:
            term.send_string(string)


# Host-only blank lines above AND below the TUI (not sent to the PTY).
# Pad *below* alone left rest_y=0, so ST could only pan dy>0 (one direction).
# Pad *above* gives headroom for dy<0 (finger-down / content-down). Rest
# viewport sits at the top of the real content so both drags produce signal.
_HOST_SCROLL_PAD_LINES = 12  # each side


def _append_host_scroll_pad(text):
    """Wrap TUI text in host-only pads so trackpad can pan both ways."""
    if text is None:
        text = ""
    if text and not text.endswith("\n"):
        text += "\n"
    pad = "\n" * _HOST_SCROLL_PAD_LINES
    return pad + text + pad


def _host_rest_y(view):
    """Viewport y that shows the top of real TUI content (below top pad)."""
    lh = view.line_height() or 12.0
    return float(_HOST_SCROLL_PAD_LINES) * lh


def _pin_viewport_rest(view, rest=None, term=None):
    """Pin to host rest_y only if the viewport drifted (avoids per-frame thrash)."""
    if rest is None:
        rest = _host_rest_y(view)
    try:
        cur = view.viewport_position()[1]
        if abs(cur - rest) > 1.0:
            view.set_viewport_position((0.0, rest), False)
        if term is not None:
            term._last_vp_y = rest
    except Exception:
        pass


def _real_content_height(view):
    """layout height of real terminal content (excludes both host pads)."""
    le = view.layout_extent()
    lh = view.line_height() or 12.0
    return max(0.0, float(le[1]) - 2 * _HOST_SCROLL_PAD_LINES * lh)


def _scroll_to_bottom(view):
    """Jump the viewport to the bottom of real terminal content (not the pad).

    Called on user input so typing brings the user back to the prompt after
    scrolling up to read scrollback. No-op when real content fits the viewport.
    """
    ve = view.viewport_extent()
    lh = view.line_height() or 12.0
    top = _host_rest_y(view)
    real_h = _real_content_height(view)
    if real_h > ve[1]:
        # Bottom of real content = top pad + real_h
        view.set_viewport_position((0.0, top + real_h - ve[1]), False)
    else:
        view.set_viewport_position((0.0, top), False)


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
            # View still tagged as a terminal but PTY owner is gone (reload /
            # orphaned tab). Without this log, keys appear to "do nothing".
            if self.view.settings().get(_VIEW_SETTING):
                print(
                    f"[ai_terminal] keypress dropped: no PTY for view "
                    f"{self.view.id()} ({self.view.name()!r}) — tab is orphaned; "
                    f"close it and open a new terminal"
                )
            return
        if not term.pty.is_alive():
            print(
                f"[ai_terminal] keypress dropped: PTY dead for "
                f"{self.view.name()!r} (view {self.view.id()})"
            )
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
            # Viewport writes (scroll_to_bottom) must NOT run on keys that only
            # move within the TUI or scrollback. set_viewport_position on
            # Windows can recompute layout and, with the layout watcher, fire
            # PTY resizes that fight Claude's cursor (left/right feel "dead"
            # for a stroke or two). Printable input still re-engages follow.
            _NO_SCROLL_KEYS = frozenset((
                "pageup", "pagedown", "home", "end",
                "left", "right", "up", "down",
            ))
            kl = key.lower()
            # Optimistic caret: Grok often leaves hardware x on the last glyph
            # for a frame after Space/printables — host block looked stuck on
            # the word until the next key. Advance display immediately.
            if not ctrl and not alt:
                if kl in _NO_SCROLL_KEYS or kl in (
                    "escape", "enter", "tab", "insert", "delete",
                ):
                    _clear_optimistic_caret(term.screen)
                elif kl == "backspace":
                    if _note_optimistic_insert(term.screen, -1):
                        term.screen.dirty = True
                        _schedule_render(term)
                elif kl == "space" or (len(key) == 1 and key.isprintable()):
                    if _note_optimistic_insert(term.screen, +1):
                        term.screen.dirty = True
                        _schedule_render(term)
            # Fullscreen / mouse-tracking TUIs (Junie, Grok): never yank the
            # viewport on every printable — that fought mid-line caret and
            # made the next char land at EOL. Pin to rest instead.
            tui = bool(
                term.screen.alt_screen or term.screen.mouse_tracking
            )
            if kl not in _NO_SCROLL_KEYS:
                term._auto_follow = True
                if tui:
                    # Only re-pin when drifted; set_viewport every key on Windows
                    # forces layout work and feels like lag/jumps on Grok.
                    try:
                        rest = _host_rest_y(self.view)
                        cur = self.view.viewport_position()[1]
                        if abs(cur - rest) > 1.0:
                            self.view.set_viewport_position((0.0, rest), False)
                        term._last_vp_y = rest
                    except Exception:
                        pass
                else:
                    _scroll_to_bottom(self.view)
                    term._last_vp_y = self.view.viewport_position()[1]
            term.send_string(code)


class AiTerminalRenderCommand(sublime_plugin.TextCommand):
    """Replace the whole view with the current screen snapshot on the main thread.

    No key/menu/palette binding; invoked programmatically.

    fast_caret: when only the host cursor glyph moved (optimistic typing),
    patch the few changed characters instead of replacing the whole buffer and
    rebuild only the host-cursor region. Keeps ST responsive under burst keys.
    """

    def run(
        self,
        edit,
        text="",
        cursor=None,
        cursor_offset=-1,
        regions=None,
        fast_caret=False,
    ):
        view = self.view
        view.set_read_only(False)
        # Only re-pin to the bottom if the user is already near it, so scrolling
        # up to read scrollback isn't yanked back on the next 40ms render.
        vp = view.viewport_position()
        ve = view.viewport_extent()
        lh = view.line_height() or 20

        patched = False
        if fast_caret and text and view.size() == len(text):
            # Diff live buffer vs new frame; host █ move is 1–2 cells.
            cur = view.substr(sublime.Region(0, view.size()))
            if len(cur) == len(text):
                diffs = []
                for i, (a, b) in enumerate(zip(cur, text)):
                    if a != b:
                        diffs.append(i)
                        if len(diffs) > 4:
                            break
                if diffs and len(diffs) <= 4:
                    for i in diffs:
                        view.replace(edit, sublime.Region(i, i + 1), text[i])
                    patched = True
                    # Host cursor region only — full colour runs are still valid.
                    host_rs = []
                    for begin, end, scope in regions or []:
                        if scope == _HOST_CURSOR_SCOPE:
                            host_rs.append(sublime.Region(begin, end))
                    key = _COLOR_KEY_PREFIX + _HOST_CURSOR_SCOPE
                    if host_rs:
                        view.add_regions(
                            key, host_rs, scope=_HOST_CURSOR_SCOPE,
                            flags=sublime.DRAW_NO_OUTLINE,
                        )
                    else:
                        view.erase_regions(key)

        if not patched:
            view.replace(edit, sublime.Region(0, view.size()), text)
            # Re-apply colour regions every frame: view.replace invalidates the old
            # regions, and add_regions with the same key replaces what was there.
            _apply_color_regions(view, regions or [])

        rest = _host_rest_y(view)
        real_h = _real_content_height(view)
        near_bottom = (vp[1] + ve[1]) >= (rest + real_h - lh * 2)
        content_fits = real_h <= ve[1] + 0.5
        term = _Terminal.from_id(view.id())
        tui_owns_scroll = bool(
            term is not None
            and (term.screen.alt_screen or term.screen.mouse_tracking)
        )
        if term is not None and not tui_owns_scroll:
            if vp[1] < term._last_vp_y - lh * 1.5:
                term._auto_follow = False
            if near_bottom:
                term._auto_follow = True
        do_follow = (
            (term is not None and term._auto_follow)
            if term is not None
            else near_bottom
        )
        # View rows: [top pad][TUI rows…][bottom pad]. Prefer absolute
        # cursor_offset (includes top pad) so mid-line ST selection matches
        # the block highlight and the next keystroke stays in place.
        pad = _HOST_SCROLL_PAD_LINES
        sel = view.sel()
        if cursor_offset is not None and int(cursor_offset) >= 0:
            pos = min(int(cursor_offset), view.size())
            sel.clear()
            sel.add(sublime.Region(pos, pos))
            if tui_owns_scroll:
                _pin_viewport_rest(view, rest, term)
            elif do_follow and not content_fits:
                _scroll_to_bottom(view)
                if term is not None:
                    term._last_vp_y = view.viewport_position()[1]
        elif cursor is not None:
            last_real = max(0, view.rowcol(view.size())[0] - pad)
            row = min(int(cursor[0]) + pad, last_real)
            line_start = view.text_point(row, 0)
            line_end = view.line(line_start).b
            pos = min(line_start + int(cursor[1]), line_end)
            sel.clear()
            sel.add(sublime.Region(pos, pos))
            if tui_owns_scroll:
                _pin_viewport_rest(view, rest, term)
            elif do_follow and not content_fits:
                _scroll_to_bottom(view)
                if term is not None:
                    term._last_vp_y = view.viewport_position()[1]
        else:
            if tui_owns_scroll:
                _pin_viewport_rest(view, rest, term)
            else:
                last_real = max(0, view.rowcol(view.size())[0] - pad)
                pt = view.text_point(last_real, 0)
                sel.clear()
                sel.add(sublime.Region(pt, pt))
                if do_follow and not content_fits:
                    _scroll_to_bottom(view)
                    if term is not None:
                        term._last_vp_y = view.viewport_position()[1]
        if content_fits or tui_owns_scroll:
            _pin_viewport_rest(view, rest, term)


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


class AiTerminalTrackpadScrollCommand(sublime_plugin.TextCommand):
    """Receive mouse-wheel / two-finger trackpad via the mousemap.

    ST's Default (Windows) mousemap does not bind bare scroll_up/scroll_down;
    the core pans the view instead, and scroll_lines is often never fired (or
    fires once then dies when content fits). User mousemap routes those buttons
    here. On a terminal view: forward to the PTY and pin the viewport. On any
    other view: fall through to native scroll_lines so normal editors still
    scroll.
    """

    def run(self, edit, direction="up", amount=3.0):
        view = self.view
        try:
            amt = abs(float(amount))
        except (TypeError, ValueError):
            amt = 3.0
        if amt <= 0:
            amt = 1.0
        # ST scroll_lines: positive = content moves down = "scroll up"
        signed = amt if direction == "up" else -amt

        if not view.settings().get(_VIEW_SETTING):
            view.run_command("scroll_lines", {"amount": signed})
            return

        term = _Terminal.from_id(view.id())
        if term is None:
            view.run_command("scroll_lines", {"amount": signed})
            return

        _route_mouse_wheel(view, term, signed)
        _pin_terminal_viewport(view, term)
        sublime.set_timeout(
            lambda v=view, t=term: _pin_terminal_viewport(v, t), 0
        )


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
        with _term_lock():
            items = list(_term_registry().items())
        for _vid, term in items:
            view = term.view
            if not view.is_valid():
                continue
            # No automatic resize. Any resize (grow or shrink) triggers the
            # TUI to rewrap/replay its content, and on a long conversation that
            # replay loops forever via viewport_extent fluctuation. See
            # _trigger_resize_for docstring. Resize happens at spawn only.
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


def _vp_pan_to_tui_scroll(view, term, dy_from_rest):
    """Turn ST viewport drift from rest into PTY scroll — content-grab model.

    dy_from_rest = viewport_y - rest_y (rest = top of real TUI, below top pad).

      dy > 0  text slides UP   → reveal newer below → amount < 0
      dy < 0  text slides DOWN → reveal older above → amount > 0

    Opposite of the TUI scroll-button (view move). Both signs must work:
    rest is not y=0 so finger-down can produce dy < 0.
    """
    lh = view.line_height() or 12.0
    dy = float(dy_from_rest)
    if abs(dy) < 1.5:
        return False
    now = time.time()
    last = float(getattr(term, "_last_scroll_send_t", 0.0) or 0.0)
    if (now - last) < 0.08:
        return False
    ticks = 1 if abs(dy) < lh * 0.75 else 2
    # Content-grab: text moves with fingers.
    amount = -float(ticks) if dy > 0 else float(ticks)
    term._last_user_pan_t = now
    try:
        n = int(getattr(term, "_vp_pan_log_n", 0) or 0)
        if n < 8:
            print(
                f"[ai_terminal] content-grab pan→TUI "
                f"dy_rest={dy:.1f}px steps={ticks} "
                f"({'older' if amount > 0 else 'newer'})"
            )
            term._vp_pan_log_n = n + 1
        _route_mouse_wheel(view, term, amount)
        return True
    except Exception as e:
        print(f"[ai_terminal] vp-pan scroll failed: {e}")
        return False


def _clamp_vp_loop():
    global _clamp_token
    try:
        for _vid, term in list(_term_registry().items()):
            v = term.view
            if not v or not v.is_valid():
                continue
            # Every ai_terminal view: trackpad = core pan. Convert + pin.
            # (Previously gated on alt/mouse only; force_main_screen makes
            # alt_screen False even under Grok, and a missed mouse mode left
            # pure pan with no PTY traffic — matches empty wheel casts.)
            try:
                v.settings().set("scroll_past_end", True)
            except Exception:
                pass
            try:
                vp = v.viewport_position()
                rest = _host_rest_y(v)
                dy_rest = float(vp[1]) - rest
                dx = float(vp[0])
            except Exception:
                continue

            tui_like = bool(
                term.screen.alt_screen
                or term.screen.mouse_tracking
            )
            try:
                le = v.layout_extent()
                ve = v.viewport_extent()
                lh = v.line_height() or 12.0
                # Near-fit relative to real content (pads always add height).
                near_fit = _real_content_height(v) <= ve[1] + lh * 2
            except Exception:
                near_fit = False
                lh = 12.0

            if tui_like or near_fit:
                if abs(dy_rest) >= 1.5:
                    _vp_pan_to_tui_scroll(v, term, dy_rest)
                if abs(dy_rest) >= 0.5 or abs(dx) >= 0.5:
                    v.set_viewport_position((0.0, rest), False)
                continue

            # Tall scrollback shell: only kill tiny overflow dips, don't steal
            # real user scrollback browsing.
            if le[1] - ve[1] <= lh and (dx != 0.0 or abs(dy_rest) >= 0.5):
                v.set_viewport_position((0.0, rest), False)
    except Exception as e:
        print(f"[ai_terminal] clamp loop error: {e}")
    # 8ms: catch the brief pan before the next paint eats it
    _clamp_token = sublime.set_timeout(_clamp_vp_loop, 8)


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
    _clamp_token = sublime.set_timeout(_clamp_vp_loop, 8)
    print("[ai_terminal] loaded (trackpad pan→TUI scroll armed)")


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
