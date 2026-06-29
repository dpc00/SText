"""dedup_logs.py â€" remove duplicate sections from Ai conversation logs.

When the ai_tab_manager plugin reloads (e.g. after a file save), it
sometimes re-logs a block of buffer content that was already written. This
script finds those duplicate blocks and removes the second occurrence.

Algorithm:
  1. Slide a window of SEED_WINDOW lines across the file, hashing each window.
  2. Any hash that appears more than once is a duplicate *candidate*.
  3. For each candidate pair (i, j) where i < j, extend the match forward and
     backward to find the full duplicate block.
  4. If the block is at least MIN_BLOCK lines, mark the second occurrence for
     removal.
  5. Write the cleaned file in-place (original backed up as .bak).

Tuning:
  SEED_WINDOW  â€" lines that must match to trigger candidate check (default 8).
                 Lower = catches shorter dups but more false positives.
  MIN_BLOCK    â€" minimum block length to actually remove (default 20).
                 Prevents removing coincidentally repeated short phrases.
"""

import glob
import os
import sys
from pathlib import Path
from typing import List, Tuple, Dict, Set

# Force UTF-8 output on Windows so Unicode log content doesn't crash prints
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

SEED_WINDOW = 8
MIN_BLOCK = 20
LOG_DIR = Path.home() / ".cache" / "claude-logs"


def deduplicate(lines: List[str]) -> Tuple[List[str], List[Tuple[int, int]]]:
    """Return (cleaned_lines, removed_ranges) where each range is (start, end) line numbers."""
    n = len(lines)
    if n < SEED_WINDOW * 2:
        return lines, []

    # Build index: tuple-of-lines -> [positions]
    index: dict[tuple, list[int]] = {}
    for i in range(n - SEED_WINDOW + 1):
        key = tuple(lines[i : i + SEED_WINDOW])
        index.setdefault(key, []).append(i)

    to_remove: set[int] = set()
    removed_ranges: list[tuple[int, int]] = []

    for positions in index.values():
        if len(positions) < 2:
            continue

        for pi in range(len(positions) - 1):
            i = positions[pi]
            j = positions[pi + 1]

            # Skip if either anchor is already inside a removed block
            if i in to_remove or j in to_remove:
                continue

            # Double-check content still matches (index built before removals)
            if lines[i : i + SEED_WINDOW] != lines[j : j + SEED_WINDOW]:
                continue

            # Extend forward
            fwd = SEED_WINDOW
            while (
                i + fwd < n
                and j + fwd < n
                and i + fwd < j  # first block must not reach second
                and lines[i + fwd] == lines[j + fwd]
            ):
                fwd += 1

            # Extend backward (without letting the blocks overlap)
            bwd = 0
            while (
                i - bwd - 1 >= 0
                and j - bwd - 1 >= 0
                and j - bwd - 1 > i + fwd  # blocks must not touch
                and lines[i - bwd - 1] == lines[j - bwd - 1]
            ):
                bwd += 1

            block_len = bwd + fwd
            if block_len < MIN_BLOCK:
                continue

            dup_start = j - bwd
            dup_end = j + fwd  # exclusive

            for k in range(dup_start, dup_end):
                to_remove.add(k)

            removed_ranges.append((dup_start, dup_end - 1))

    cleaned = [line for i, line in enumerate(lines) if i not in to_remove]
    removed_ranges.sort()
    return cleaned, removed_ranges


import re as _re

_TRAIL_JUNK = _re.compile(r"[\s─-╿▀-▟]+$")
# Wide status-bar lines: non-space, big gap (20+ spaces), non-space
_STATUS_BAR_GAP = _re.compile(r"\S\s{20,}\S")
# Narrow status-bar content patterns (may be merged with no big gap)
_STATUS_BAR_CONTENT = _re.compile(
    r"Session:\s+\d|Ctx Used:\s+[\d.]|Cost:\s+\$[\d]|\bMem:\s+[\d.]"
)
# "â† for agents" / "â†’ for agents" prefix that may be glued to real content
_AGENT_PREFIX = _re.compile(r"^\s*[â†â†’]\s+for agents\s*")


def _clean_line(line: str) -> str:
    """Strip trailing whitespace and terminal box-drawing/block padding.
    Returns empty string (or prefix-stripped content) for status-bar lines."""
    stripped = _TRAIL_JUNK.sub("", line)
    # Strip "â† for agents" prefix; keep whatever follows (may be real content)
    stripped = _AGENT_PREFIX.sub("", stripped)
    # Drop wide padded status-bar lines
    if len(stripped) > 100 and _STATUS_BAR_GAP.search(stripped):
        return ""
    # Drop lines whose content is entirely session/cost/ctx status info
    if _STATUS_BAR_CONTENT.search(stripped) and len(stripped) > 60:
        return ""
    return stripped + ("\n" if line.endswith("\n") else "")


def process_file(path: Path, dry_run: bool = False) -> None:
    raw = path.read_text(encoding="utf-8", errors="replace")
    # Normalize bare \r (terminal line-overwrite) to \n before splitting
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_clean_line(l) for l in raw.splitlines(keepends=True)]
    original_count = len(lines)

    all_ranges: list[tuple[int, int]] = []
    total_removed = 0
    current = lines
    passes = 0
    for _ in range(200):  # safety cap; each pass removes >=1 block
        cleaned, ranges = deduplicate(current)
        if not ranges:
            break
        total_removed += len(current) - len(cleaned)
        all_ranges.extend(ranges)
        current = cleaned
        passes += 1

    if not all_ranges:
        print(f"  {path.name}: no duplicates found ({original_count} lines)")
        return

    pass_word = "pass" if passes == 1 else "passes"
    print(
        f"  {path.name}: {original_count} lines -> {len(current)} lines "
        f"(removed {total_removed} lines in {len(all_ranges)} block(s), {passes} {pass_word})"
    )
    for start, end in all_ranges[:10]:
        preview = lines[start].rstrip()[:60] if start < len(lines) else "..."
        print(f"    lines {start + 1}-{end + 1}: '{preview}...'")

    if dry_run:
        return

    backup = path.with_suffix(".log.bak")
    backup.unlink(missing_ok=True)
    path.rename(backup)
    path.write_bytes("".join(current).encode("utf-8"))
    print(f"    backed up original to {backup.name}")


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("DRY RUN â€ no files will be changed")

    log_files = sorted(LOG_DIR.glob("*.log"))
    if not log_files:
        print(f"No .log files found in {LOG_DIR}")
        return

    print(f"Scanning {len(log_files)} log file(s) in {LOG_DIR}\n")
    for path in log_files:
        process_file(path, dry_run=dry_run)


if __name__ == "__main__":
    main()
