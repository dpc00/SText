"""font_classifier.py — Build a monospace font database from TTF/OTF post tables.

Reads isFixedPitch directly from each font file (ground truth).
Run once; output is read by ai_settings_server.py.

Usage:
    python font_classifier.py [--out PATH]
    Default output: ~/.claude/font_db.json
"""

import json
import os
import struct
import sys
import winreg
from pathlib import Path

DEFAULT_OUT = Path.home() / ".claude" / "font_db.json"
FONTS_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def _iter_registry_fonts():
    """Yield (display_name, filepath) from Windows font registry, both system and user."""
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
        except OSError:
            continue
        i = 0
        while True:
            try:
                name, path, _ = winreg.EnumValue(key, i)
                i += 1
            except OSError:
                break
            display = name.split(" (")[0].strip()
            if not display or display.startswith('@'):
                continue
            if not os.path.isabs(path):
                path = str(FONTS_DIR / path)
            if os.path.isfile(path):
                yield display, path
        winreg.CloseKey(key)


def _post_is_fixed(path):
    """
    Return True/False for isFixedPitch from the font's post table.
    Returns None if the file can't be parsed (skip it).

    post table layout (all big-endian):
      offset 0:  version        Fixed (4 bytes)
      offset 4:  italicAngle    Fixed (4 bytes)
      offset 8:  underlinePos   FWord (2 bytes)
      offset 10: underlineThick FWord (2 bytes)
      offset 12: isFixedPitch   uint32 — nonzero = monospace
    """
    try:
        with open(path, 'rb') as f:
            tag = f.read(4)

            if tag == b'ttcf':
                # TrueType Collection: skip to first font's offset table
                f.read(4)   # majorVersion(2) + minorVersion(2)
                f.read(4)   # numFonts
                offset = struct.unpack('>I', f.read(4))[0]
                f.seek(offset)
                tag = f.read(4)  # sfVersion of first font

            if tag not in (b'\x00\x01\x00\x00', b'true', b'OTTO', b'typ1'):
                return None

            num_tables = struct.unpack('>H', f.read(2))[0]
            f.read(6)  # searchRange, entrySelector, rangeShift

            post_offset = None
            for _ in range(num_tables):
                t, _csum, off, _len = struct.unpack('>4sIII', f.read(16))
                if t == b'post':
                    post_offset = off
                    break

            if post_offset is None:
                return None

            f.seek(post_offset + 12)
            return struct.unpack('>I', f.read(4))[0] != 0
    except Exception:
        return None


def build(out_path=DEFAULT_OUT):
    seen = {}  # display_name -> path (last one wins for duplicates)
    for name, path in _iter_registry_fonts():
        seen[name] = path

    all_fonts = sorted(seen)
    mono = []
    skipped = 0

    for name in all_fonts:
        result = _post_is_fixed(seen[name])
        if result is None:
            skipped += 1
        elif result:
            mono.append(name)

    db = {"all": all_fonts, "mono": mono}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(db, indent=2), encoding="utf-8")

    print(f"Total fonts : {len(all_fonts)}")
    print(f"Monospace   : {len(mono)}")
    print(f"Skipped     : {skipped}")
    print(f"Written to  : {out_path}")
    return db


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(DEFAULT_OUT))
    args = p.parse_args()
    build(args.out)
