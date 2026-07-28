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

_APP_MODEKEY_MAP = {
    "down": "\x1bOB",
    "up": "\x1bOA",
    "right": "\x1bOC",
    "left": "\x1bOD",
}

_CTRLKEY_MAP = {
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

_ALTKEY_MAP = {
    "up": "\x1b[1;3A",
    "down": "\x1b[1;3B",
    "right": "\x1b[1;3C",
    "left": "\x1b[1;3D",
}

_SHIFTKEY_MAP = {
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
    if application_mode and key in _APP_MODEKEY_MAP:
        return _APP_MODEKEY_MAP[key]
    if key in KEY_MAP:
        return KEY_MAP[key]
    return key


def get_ctrl_key_code(key):
    key = key.lower()
    if key in _CTRLKEY_MAP:
        return _CTRLKEY_MAP[key]
    if len(key) == 1 and "a" <= key <= "z":
        return chr(ord(key) - ord("a") + 1)
    return get_key_code(key)


def get_alt_key_code(key):
    key_lo = key.lower()
    if key_lo in _ALTKEY_MAP:
        return _ALTKEY_MAP[key_lo]
    return "\x1b" + get_key_code(key)


def get_shift_key_code(key):
    key = key.lower()
    if key in _SHIFTKEY_MAP:
        return _SHIFTKEY_MAP[key]
    if key in KEY_MAP:
        return KEY_MAP[key]
    return key.upper()


def translate_key(key, ctrl=False, alt=False, shift=False):
    if ctrl:
        return get_ctrl_key_code(key)
    if alt:
        return get_alt_key_code(key)
    if shift:
        return get_shift_key_code(key)
    return get_key_code(key)


