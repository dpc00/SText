"""Key name -> terminal byte sequences (Terminus-compatible).

Pure Python — no Sublime imports.
"""

KEY_MAP = {
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

APP_MODE_KEY_MAP = {
    "down": "\x1bOB",
    "up": "\x1bOA",
    "right": "\x1bOC",
    "left": "\x1bOD",
}

CTRL_KEY_MAP = {
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

ALT_KEY_MAP = {
    "up": "\x1b[1;3A",
    "down": "\x1b[1;3B",
    "right": "\x1b[1;3C",
    "left": "\x1b[1;3D",
}

SHIFT_KEY_MAP = {
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


def get_key_code(key, application_mode=False):
    if application_mode and key in APP_MODE_KEY_MAP:
        return APP_MODE_KEY_MAP[key]
    if key in KEY_MAP:
        return KEY_MAP[key]
    return key


def get_ctrl_key_code(key):
    key = key.lower()
    if key in CTRL_KEY_MAP:
        return CTRL_KEY_MAP[key]
    if len(key) == 1 and "a" <= key <= "z":
        return chr(ord(key) - ord("a") + 1)
    return get_key_code(key)


def get_alt_key_code(key):
    key_lo = key.lower()
    if key_lo in ALT_KEY_MAP:
        return ALT_KEY_MAP[key_lo]
    return "\x1b" + get_key_code(key)


def get_shift_key_code(key):
    key = key.lower()
    if key in SHIFT_KEY_MAP:
        return SHIFT_KEY_MAP[key]
    if key in KEY_MAP:
        return KEY_MAP[key]
    return key.upper()


def translate_key(key, ctrl=False, alt=False, shift=False, application_mode=False):
    if ctrl:
        return get_ctrl_key_code(key)
    if alt:
        return get_alt_key_code(key)
    if shift:
        return get_shift_key_code(key)
    return get_key_code(key, application_mode=application_mode)


# ─── Win32-input-mode (DEC private mode 9001) ──────────────────────────────
# Apps built on newer Node/Ink TUI stacks (confirmed: Qwen Code) enable this
# on Windows for reliable key handling and then IGNORE plain xterm sequences
# entirely -- KEY_MAP above becomes a no-op once the app has switched. Format
# (Windows Terminal / ConPTY spec): CSI Vk;Sc;Uc;Kd;Cs;Rc _
#   Vk = virtual-key code (decimal)      Sc = scan code (decimal, best-effort)
#   Uc = Unicode char as decimal (0 = none)   Kd = 1 key-down / 0 key-up
#   Cs = control-key-state bitmask       Rc = repeat count
# Control-key-state bits (win32 console INPUT_RECORD.dwControlKeyState):
RIGHT_ALT_PRESSED = 0x0001
LEFT_ALT_PRESSED = 0x0002
RIGHT_CTRL_PRESSED = 0x0004
LEFT_CTRL_PRESSED = 0x0008
SHIFT_PRESSED = 0x0010
ENHANCED_KEY = 0x0100

# name -> (Vk, Sc). Scan codes are standard US PS/2 Set-1; apps mostly key
# off Vk + Uc so exact Sc correctness matters less. Nav/arrow/edit keys are
# "enhanced" (0xE0-prefixed) on real hardware -- ENHANCED_KEY is added for
# those in _win32_cs below, matching genuine Windows keyboard input.
_WIN32_VK = {
    "enter": (0x0D, 28), "tab": (0x09, 15), "escape": (0x1B, 1),
    "backspace": (0x08, 14), "space": (0x20, 57),
    "up": (0x26, 72), "down": (0x28, 80), "left": (0x25, 75), "right": (0x27, 77),
    "home": (0x24, 71), "end": (0x23, 79),
    "pageup": (0x21, 73), "pagedown": (0x22, 81),
    "insert": (0x2D, 82), "delete": (0x2E, 83),
    "f1": (0x70, 59), "f2": (0x71, 60), "f3": (0x72, 61), "f4": (0x73, 62),
    "f5": (0x74, 63), "f6": (0x75, 64), "f7": (0x76, 65), "f8": (0x77, 66),
    "f9": (0x78, 67), "f10": (0x79, 68), "f11": (0x7A, 87), "f12": (0x7B, 88),
}
# Keys the real OS marks as "enhanced" (extended 0xE0 scan-code prefix).
_WIN32_ENHANCED = frozenset((
    "up", "down", "left", "right", "home", "end",
    "pageup", "pagedown", "insert", "delete",
))
# Uc (Unicode char) for named non-printable keys, when no modifier changes it.
_WIN32_UC = {
    "enter": 13, "tab": 9, "escape": 27, "backspace": 8, "space": 32,
}
# US QWERTY punctuation -> (Vk, Sc). Windows OEM Vk codes are keyboard-layout
# specific; this covers the standard US layout. Unmapped punctuation falls
# back to Vk=0 -- Uc (the actual character) still carries, which is what
# text-input handling primarily reads.
_WIN32_LETTER_SC = {
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34, "h": 35,
    "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49, "o": 24, "p": 25,
    "q": 16, "r": 19, "s": 31, "t": 20, "u": 22, "v": 47, "w": 17, "x": 45,
    "y": 21, "z": 44,
}
_WIN32_DIGIT_SC = {
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
}
_WIN32_PUNCT_VK = {
    "`": (0xC0, 41), "-": (0xBD, 12), "=": (0xBB, 13),
    "[": (0xDB, 26), "]": (0xDD, 27), "\\": (0xDC, 43),
    ";": (0xBA, 39), "'": (0xDE, 40), ",": (0xBC, 51),
    ".": (0xBE, 52), "/": (0xBF, 53),
}


def _win32_cs(ctrl, alt, shift):
    cs = 0
    if ctrl:
        cs |= LEFT_CTRL_PRESSED
    if alt:
        cs |= LEFT_ALT_PRESSED
    if shift:
        cs |= SHIFT_PRESSED
    return cs


def encode_win32_key(key, ctrl=False, alt=False, shift=False):
    """CSI Vk;Sc;Uc;Kd;Cs;Rc _ for one key-down event, or "" if unencodable."""
    kl = key.lower()
    cs = _win32_cs(ctrl, alt, shift)
    if kl in _WIN32_ENHANCED:
        cs |= ENHANCED_KEY

    if kl in _WIN32_VK:
        vk, sc = _WIN32_VK[kl]
        uc = _WIN32_UC.get(kl, 0)
        if ctrl and len(kl) == 1 and "a" <= kl <= "z":
            uc = ord(kl) - ord("a") + 1
        elif kl == "backspace" and ctrl:
            uc = 127  # BS with Ctrl held conventionally sends DEL
        return "\x1b[%d;%d;%d;1;%d;1_" % (vk, sc, uc, cs)

    if len(key) == 1:
        if "a" <= kl <= "z":
            vk = ord(kl.upper())
            sc = _WIN32_LETTER_SC.get(kl, 0)
            uc = ord(key.upper()) if shift else ord(key)
            if ctrl:
                uc = ord(kl) - ord("a") + 1
            return "\x1b[%d;%d;%d;1;%d;1_" % (vk, sc, uc, cs)
        if "0" <= key <= "9":
            vk = ord(key)
            sc = _WIN32_DIGIT_SC.get(key, 0)
            uc = ord(key)
            return "\x1b[%d;%d;%d;1;%d;1_" % (vk, sc, uc, cs)
        if kl in _WIN32_PUNCT_VK:
            vk, sc = _WIN32_PUNCT_VK[kl]
            return "\x1b[%d;%d;%d;1;%d;1_" % (vk, sc, ord(key), cs)
        # Unknown punctuation/symbol: Vk=0, character still carried via Uc.
        return "\x1b[0;0;%d;1;%d;1_" % (ord(key), cs)

    return ""
