"""ai_logger_watcher.py — standalone 20Hz watcher (Python fallback for ai_logger_watcher.exe).

Polls snapshot_file at 20 Hz.  When content changes, writes a timestamped
full snapshot to log_file so the log faithfully records what was on screen.

Run: python ai_logger_watcher.py <snapshot_file> <log_file> [min_interval_ms]

ai_logger.py launches this automatically if ai_logger_watcher.exe is absent.
"""

import datetime
import os
import sys
import time
import zlib


def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _find_change(prev: bytes, curr: bytes) -> int:
    """Return index of first differing byte."""
    end = min(len(prev), len(curr))
    for i in range(end):
        if prev[i] != curr[i]:
            return i
    return end  # difference is at the shorter tail


def _write_log(log_path: str, content: bytes, change_at: int) -> None:
    now = datetime.datetime.now()
    header = f"\n[{now.strftime('%H:%M:%S')}.{now.microsecond // 1000:03d} change@{change_at}]\n"
    with open(log_path, "ab") as f:
        f.write(header.encode())
        f.write(content)
        if not content.endswith(b"\n"):
            f.write(b"\n")


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <snapshot_file> <log_file> [min_interval_ms]",
              file=sys.stderr)
        sys.exit(1)

    snap_path = sys.argv[1]
    log_path = sys.argv[2]
    min_interval = float(sys.argv[3]) / 1000.0 if len(sys.argv) >= 4 else 0.2
    poll = 0.05  # 20 Hz

    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    prev = b""
    prev_crc = None
    last_write = 0.0

    while True:
        try:
            with open(snap_path, "rb") as f:
                curr = f.read()
        except OSError:
            time.sleep(poll)
            continue

        curr_crc = _crc32(curr)
        if curr_crc != prev_crc or len(curr) != len(prev):
            now = time.monotonic()
            if (now - last_write) >= min_interval:
                change_at = _find_change(prev, curr)
                _write_log(log_path, curr, change_at)
                last_write = now
            prev = curr
            prev_crc = curr_crc

        time.sleep(poll)


if __name__ == "__main__":
    main()
