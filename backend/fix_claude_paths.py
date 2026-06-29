"""fix_claude_paths.py -- patch .claude paths to .cache in SText plugins and move files.

Run this with ST closed (so ai_logger.py is not active):
    python fix_claude_paths.py
"""

import re
import shutil
from pathlib import Path

HOME = Path.home()
PROJECTS = HOME / "projects" / "SText"
ST_PACKAGES = HOME / "AppData" / "Roaming" / "Sublime Text" / "Packages" / "User"

# ---------------------------------------------------------------------------
# 1. Path substitutions to apply in source files
# ---------------------------------------------------------------------------

SUBSTITUTIONS = [
    # ai_logger.py
    (
        PROJECTS / "ai_logger.py",
        [
            (
                r'_STATE_FILE\s*=\s*str\(Path\.home\(\) / "\.claude" / "ai_logger_state\.json"\)',
                '_STATE_FILE       = str(Path.home() / ".cache" / "ai_logger_state.json")',
            ),
            (
                r'_DIAGNOSTICS_FILE\s*=\s*str\(Path\.home\(\) / "\.claude" / "ai_diagnostics\.log"\)',
                '_DIAGNOSTICS_FILE = str(Path.home() / ".cache" / "ai_diagnostics.log")',
            ),
        ],
    ),
    # ai_tab_manager.py
    (
        PROJECTS / "ai_tab_manager.py",
        [
            (
                r'_LOG_DIR\s*=\s*str\(Path\.home\(\) / "\.claude" / "conversation_logs"\)',
                '_LOG_DIR = str(Path.home() / ".cache" / "claude-logs")',
            ),
            (
                r'_DIAGNOSTICS_FILE\s*=\s*str\(Path\.home\(\) / "\.claude" / "ai_diagnostics\.log"\)',
                '_DIAGNOSTICS_FILE = str(Path.home() / ".cache" / "ai_diagnostics.log")',
            ),
            (
                r'Path\.home\(\) / "\.claude" / "buffer_exports"',
                'Path.home() / ".cache" / "claude-buffer-exports"',
            ),
        ],
    ),
    # dedup_logs.py
    (
        PROJECTS / "dedup_logs.py",
        [
            (
                r'LOG_DIR\s*=\s*Path\.home\(\) / "\.claude" / "conversation_logs"',
                'LOG_DIR = Path.home() / ".cache" / "claude-logs"',
            ),
        ],
    ),
]

# ---------------------------------------------------------------------------
# 2. Files/dirs to move from .claude to .cache
# ---------------------------------------------------------------------------

MOVES = [
    (HOME / ".claude" / "ai_logger_state.json",  HOME / ".cache" / "ai_logger_state.json"),
    (HOME / ".claude" / "ai_diagnostics.log",    HOME / ".cache" / "ai_diagnostics.log"),
    (HOME / ".claude" / "claude_diagnostics.log", HOME / ".cache" / "claude_diagnostics.log"),
    (HOME / ".claude" / "font_db.json",          HOME / ".cache" / "font_db.json"),
]

# Move any remaining files in old conversation_logs dir
OLD_LOGS = HOME / ".claude" / "conversation_logs"
NEW_LOGS = HOME / ".cache" / "claude-logs"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def patch_file(path: Path, subs: list) -> bool:
    text = path.read_text(encoding="utf-8")
    original = text
    for pattern, replacement in subs:
        text = re.sub(pattern, replacement, text)
    if text != original:
        path.write_text(text, encoding="utf-8")
        print(f"  patched: {path.name}")
        return True
    else:
        print(f"  no change: {path.name} (already correct or pattern not found)")
        return False

def move_file(src: Path, dst: Path):
    if not src.exists():
        print(f"  skip (not found): {src.name}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    print(f"  moved: {src.name}  ->  {dst}")

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

print("\n=== 1. Patching source files ===")
changed = []
for file_path, subs in SUBSTITUTIONS:
    if not file_path.exists():
        print(f"  not found: {file_path}")
        continue
    if patch_file(file_path, subs):
        changed.append(file_path)

print("\n=== 2. Deploying changed files to ST Packages ===")
deploy_targets = {
    PROJECTS / "ai_logger.py":     ST_PACKAGES / "ai_logger.py",
    PROJECTS / "ai_tab_manager.py": ST_PACKAGES / "ai_tab_manager.py",
}
for src, dst in deploy_targets.items():
    if src in changed:
        shutil.copy2(str(src), str(dst))
        print(f"  deployed: {src.name}")
    else:
        print(f"  skipped (unchanged): {src.name}")

print("\n=== 3. Moving files out of .claude ===")
for src, dst in MOVES:
    move_file(src, dst)

print("\n=== 4. Moving remaining logs from old conversation_logs dir ===")
if OLD_LOGS.exists():
    NEW_LOGS.mkdir(parents=True, exist_ok=True)
    moved = 0
    for f in OLD_LOGS.iterdir():
        if f.is_file():
            dst = NEW_LOGS / f.name
            if dst.exists():
                print(f"  skip (already at dest): {f.name}")
            else:
                shutil.move(str(f), str(dst))
                moved += 1
    print(f"  moved {moved} log file(s)")
    try:
        OLD_LOGS.rmdir()
        print(f"  removed empty dir: {OLD_LOGS}")
    except OSError:
        print(f"  dir not empty, left in place: {OLD_LOGS}")
else:
    print("  old conversation_logs dir not found")

print("\nDone.")
