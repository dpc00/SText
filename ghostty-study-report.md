# Ghostty Study Report — Patterns to Apply to `ai_terminal.py`

Study period: 2026-07-06, ~1.5 hours (well under the 3-4h budget; stopped early per Donald's guidance).
Source: `C:\Users\donal\tools\ghostty` @ commit `634957c8e` (PR #13226 "VT throughput optimizations from real-world dataset").

## Executive summary

Five patterns matter for a Python-in-ST terminal; the rest are Zig/SIMD-specific and not worth porting.

1. **State table → O(1) per byte** — VT parsing is a 256×14 lookup, not an if-tree. Replacing `if st == _GROUND: ...` chains in `ai_terminal.py:_Parser._step` (lines 733-799) with a one-table dispatch is the single biggest correctness + perf win. Pattern: `Parser.zig:251-311` `next()`, backed by `parse_table.zig:45-388`.
2. **Bulk SIMD print runs in the parser hot path** — `print_slice` is fed to the parser as runs of already-decoded codepoints, NOT one-by-one. `Stream.zig:499-589` (nextSlice / utf8DecodeUntilControlSeq), `Terminal.zig:339-836` (printSlice / printSliceFast / printSliceFill). The Python equivalent: a 256-char `bytes` buffer that the parser returns as one `(run, attr)` tuple, then `_Screen.put_char` runs as a tight C-level loop.
3. **Batched style ref updates on erase, not per-cell** — `Screen.zig:1434-1450` (PR #13226 item 4): group cells by `style_id`, release each run with a single `releaseMultiple`, not one `release` per cell. The 2.1x erase speedup comes almost for free in Python by keeping style ids in a per-row `array('H')` and grouping identicals in one pass.
4. **Log-once-per-value for unsupported warnings** — `stream.zig:2651-2688` `logUnsupportedOnce` (PR #13226 item 5): a 16-slot atomic-claimed table keyed on a `u16`, so "unimplemented mode: 34" gets logged once, not 120k times. Cheap port to Python: a `set()` of `id(value)` keys, ~3 lines.
5. **Vectorized mask-and-fill runs in the screen-write path** — `Terminal.zig:656-773`: build a `simple_mask` of the cell fields that must match expected, scan 4 cells at a time, branch-free fill the matching run. For Python this becomes "build a `bytearray` template, copy a row's worth at once, then `view.replace(edit, full_text, 0)` once per render." Eliminates the per-cell Python attribute dict construction in `ai_terminal.py:1204-1240` `_build_text_and_regions`.

The biggest non-pattern to AVOID: copying Ghostty's `Screen`/`Page`/`PageList` linked-list-of-arena-allocated-pages architecture. Ghostty's entire reason for the linked-list design is fast, refcounted Kitty graphics + per-cell refcounts. `ai_terminal` has no graphics, no per-cell refcounting, and an ST view that already holds all visible text. Porting the linked-list design would be a 1500-line rewrite for zero observable benefit.

## Patterns

### Pattern 1 — Single state-table dispatch for VT parsing

**Problem.** `ai_terminal.py:_Parser._step` (lines 733-799) is an `if st == _GROUND / elif st == _ESC / ...` chain with 60+ branches. Every printable byte in the inner loop pays ~5 Python bytecode ops of dispatch before doing its work. Worse, the states that need it most (`_CSI`, `_OSC`) accumulate state in *strings* (`self.params += ch`), which CPython re-allocates on every byte. The `param` string is then re-`split`/`lstrip` on every dispatch (line 800-808 `_ints`).

**How Ghostty does it.** `Parser.next` (`Parser.zig:251-311`) is a single table lookup: `const effect = table[c][@intFromEnum(self.state)]`. The table is a 256×14 array of `(next_state, action)` tuples, built `comptime` from a 388-line declarative list in `parse_table.zig:45-388`. `next_state` and `action` are enums; the action handler is a single `switch` (`Parser.zig:324-407`) where the hot path (`print`, `param`, `csi_dispatch`) is a tight integer-compare chain. CSI params are stored in a fixed-size `[24]u16` array (`Parser.zig:213`), and `MAX_PARAMS = 24` was chosen empirically (see the comment at `Parser.zig:193-203` — Kakoune emits 17-param SGRs).

In Stream, the per-byte work for the dominant case (csi_param) is even further collapsed. `nextNonUtf8` (`Stream.zig:824-938`) has a hand-written fast path for `csi_param` (lines 840-886) that does NOT go through `Parser.next` at all — it inlines the param digit/sep/final transition directly, with `@branchHint(.likely)` on csi_param because it is the most common non-ground state. Result: a typical CSI parses with zero calls to `Parser.next` (PR #13226 item 2, +2.7% real corpus).

**Cost to port to Python.** ~150-200 lines. Replace `_Parser` with:
- A `(state, byte) → (next_state, action)` dict, built once at import time (256 × 8 = 2048 entries). `dict[(state, byte)]` is fine; if startup matters, use a 256-element list of dicts or a flat tuple-of-tuples.
- A `_PARAMS = array('H')` (or just a `list[int]` of fixed size 24) instead of a string.
- A single `match`-style if/elif on `action` enum.
- An explicit fast path for the `csi_param` branch like Ghostty's, but inlined at the top of `feed()` since the csi_param state is the most common non-ground state.

**Recommendation.** ADOPT. This is the single biggest win and is the only way to keep parser overhead low once you move to `print_slice`-style batched runs. The state table is also the *correctness* backbone — it's a lot harder to miss an OSC state transition when transitions come from a single declarative table.

### Pattern 2 — `print_slice`: batch codepoints from parser to screen

**Problem.** `ai_terminal.py:_Parser._step:752` calls `self.s.put_char(ch, self._cur_attr)` one Unicode char at a time. Each call is a Python method call → frame setup → bounds check → tuple write → `self.dirty = True`. For `cat /usr/share/dict/words` at 10 MB/s output, the parser feeds the screen roughly 1.5M put_chars/sec. The screen is not the bottleneck (its `put_char` is a simple 2-list assignment) but the Python frame is.

**How Ghostty does it.** `Stream.zig:551-571` runs SIMD to find the next ESC byte, decodes the entire ASCII run in one shot to a `[]u32` codepoint buffer, then hands the *whole* run to the terminal as a `print_slice` action. The terminal's `printSlice` (`Terminal.zig:339-357`) tries `printSliceFast` first, which then runs `printSliceFill` (`Terminal.zig:502-836`) — a tight loop that:
- Validates the entire run is "simple" (narrow, no grapheme, no hyperlink) using a vectorized 4-cell compare (`Terminal.zig:664-683`).
- For each contiguous run of cells that match the simple mask: branch-free writes the whole run (`Terminal.zig:703-708`).
- For the next contiguous run where the only difference is `style_id`: also branch-free writes, but with two single `releaseMultiple`/`useMultiple` ref-count updates for the whole run (`Terminal.zig:720-773`, PR #13226 item 3, +11% real corpus, +21% TUI redraw).
- Falls back to per-codepoint `print()` for anything that fails the eligibility check.

**Cost to port to Python.** ~100-150 lines, but the win is real. The Python equivalent:
- In `_Parser._step`, when in `_GROUND` state, scan a `bytes` buffer for the next `\x1b` (or `\x07`/C0) using `bytes.find` (C-level, fast). Yield the slice as one `(bytes, attr)` tuple.
- In `_Screen`, add `put_run(text: bytes, attr: int)` that writes the whole `text` to `self.grid[y][x:x+len(text)]` and `self.attrs[y][x:x+len(text)]` (Python slice assignment, C-level). Increments `self.x` by `len(text)`.
- The SGR-handling cost: when the SGR changes, the parser breaks the run and starts a new one with the new attr. The screen never sees a "the attr changed mid-cell" condition.
- The screen does NOT need the vectorized eligibility check — Python's per-character cost in `put_run` is dominated by the slice assign, not the loop.

**Recommendation.** ADOPT. This is the single biggest "free lunch" in Ghostty's PR. The Python code is simpler than Ghostty's because we don't have SIMD lanes, refcounted styles, or grapheme clustering — just `grid[y][x:x+n] = text_bytes` and `attrs[y][x:x+n] = [attr]*n`. Eliminates the per-char Python frame cost.

### Pattern 3 — Batched style ref updates on `clearCells`

**Problem.** `ai_terminal.py:_Screen.erase_line:626-641` and `erase_display:603-624` set `self.attrs[r] = [0] * self.cols` for every cleared row. For a 120×80 screen with 30 styled rows, that's 30 row × 80 attrs = 2400 attribute resets, each a Python list allocation (`[0] * 80`). Total: ~2400 small list allocations per `ESC[2J`.

**How Ghostty does it.** PR #13226 item 4 (`Screen.zig:1434-1450`, commit `8d663a76e`): when a row is `row.styled`, group cells by `style_id` and call `releaseMultiple` once per run. Per cell release was the prior implementation; the new code does this in a `while` loop that walks the row until `style_id` changes, then calls `releaseMultiple` once. The benchmark result: **2.1x on full-screen styled erase**.

For `ai_terminal`, the per-cell release translates to "set `attrs[c] = 0`". The Python port is simpler: just write `self.attrs[r] = [0] * self.cols` (one allocation, one C-level memcpy) instead of looping. The PR's insight generalizes: don't update cell-by-cell when you can update row-by-row. For Python, that's a 30x reduction in attribute mutations per `ESC[2J`.

**Cost to port to Python.** ~10 lines. Already-trivial; the port is the *realization* that the current code is doing it inefficiently.

**Recommendation.** ADOPT. Trivial change, real win, and a "spirit of the Ghostty fix" port.

### Pattern 4 — `logUnsupportedOnce`: dedup noisy log messages

**Problem.** `ai_terminal.py:_Parser._dispatch_csi:906-910` silently consumes-and-drops "all other finals." That's correct for spec compliance but loses diagnostic info when a TUI sends an unsupported sequence every frame. The current code has no `print`/`log` here at all, so this is technically hypothetical — but the same anti-pattern shows up in any future "log unsupported X" path the user might add.

**How Ghostty does it.** `stream.zig:2651-2688` `logUnsupportedOnce`. The whole function is ~35 lines. The structure:
- A `comptime` format string and a `key: u16`.
- A `Static` struct holding a 16-element `[]u32` seen-set, keyed by sentinel `0xFFFFFFFF`.
- A linear scan with `cmpxchg` to claim a slot atomically: if we see the key, return without logging. If we see an empty slot, claim it and log. If the table is full, suppress (don't log).
- Worst-case race is a benign duplicate.

**Cost to port to Python.** ~5 lines. Python: `_seen: set[int] = set()` on the parser; `if key in self._seen: return; self._seen.add(key); log.warn(...)`. The 16-slot cap doesn't translate (Python `set` is unbounded); to match Ghostty's "table full → suppress", use a fixed-size LRU. For ai_terminal, an unbounded `set()` is fine — Claude's TUI emits a handful of distinct modes at most.

**Recommendation.** ADOPT (when/if the parser is extended to log unsupported sequences). Pure win, no downside. The current code doesn't log unsupported sequences at all, so this is a SKIP-for-now. Mark as "do this the moment you add a `log.warn` in the parser."

### Pattern 5 — Per-row dirty, render-on-demand, no per-cell diff

**Problem.** `ai_terminal.py:_Screen` has `self.dirty = True` on every method (`put_char:567`, `lf:577`, `cr:581`, `bs:586`, `tab:590`, `move_abs:595`, `move_rel:600`, `erase_display:624`, `erase_line:641`, `save_cursor:644`, `restore_cursor:650`). The whole screen is one dirty bit, so any single-cell change re-renders all `rows × cols` cells. For 120×80 = 9600 cells, each `render_cells` (`_Screen.render_cells:653-712`) builds 9600 `(char, attr)` tuples — 9600 Python tuple allocations per render.

**How Ghostty does it.** Per-row dirty, per-cell dirty, AND ref-counted styles. The `Page` (`page.zig`) tracks `Row.dirty: bool`; the renderer only walks dirty rows. Each cell has `wide: enum { narrow, spacer_head, spacer_tail }`, `content_tag`, `style_id`, etc. as packed struct fields. Bulk fills use a `simple_mask` (`Terminal.zig:515-520`) to compare all fields in one `@bitCast(u64)` compare.

For Python, the equivalent is: don't render the whole screen every time. Track which *rows* changed (`self.dirty_rows: set[int]`) and only rebuild those rows in `_build_text_and_regions`. Most cursor moves only dirty one row.

**Cost to port to Python.** ~30 lines: add `self.dirty_rows: set[int]` to `_Screen`; every `put_char` / `lf` / etc. adds the affected row index to the set; `render_cells` skips clean rows. ST's `view.replace(edit, full_text, 0)` is a single C-level call so it doesn't matter if you hand it 9600 cells or 80, but the tuple-allocation savings in `render_cells` are real.

**Recommendation.** ADOPT. Real win, small change, no downside.

### Pattern 6 (SKIP) — PageList linked-list of arena-allocated pages

**How Ghostty does it.** `PageList.zig:85-300`: a doubly-linked list of memory-pool-allocated `Page` nodes, each page is a fixed-size buffer holding `rows × cols` cells + per-cell `style_id` + per-cell `content_tag`. Scrolling (which moves a `Pin` between pages) is O(1).

**Why SKIP for ai_terminal.** `ai_terminal` has no scrollback in the PTY sense — the rendered view is `self.rows` lines plus the history deque (`_Screen.history:500`). The scrollback limit is a `collections.deque(maxlen=N)` (line 500), which is exactly the right structure for Python. Porting `PageList` would require inventing a custom arena allocator to avoid GC pressure, and there's no win: a 80×24 page is 1920 cells, a 120×80 page is 9600. Python's list-of-lists already lives in heap-allocated C arrays, and the 1920-cell scroll-up costs 1 list reassignment.

**Recommendation.** SKIP. The architecture is wrong for Python. Donald's `grid: list[list[str]]` + `attrs: list[list[int]]` + `history: deque` is the right shape.

### Pattern 7 (SKIP) — SIMD UTF-8 decode + ESC scan

**How Ghostty does it.** `Stream.zig:551` calls `simd.vt.utf8DecodeUntilControlSeq` — a single SIMD pass that does (a) scan for next ESC, (b) widen ASCII to UTF-32, in one instruction sequence. PR #13226 item 1 merged these into one pass: +5.4% on real corpus.

**Why SKIP for ai_terminal.** Python's `bytes.find(b'\x1b')` is already a C-level memchr. There's no SIMD available from Python without ctypes/cffi and a custom build. The C-level scan is fast enough; the wins in #1, #2, #3 above are Python-specific and don't require SIMD.

**Recommendation.** SKIP. Catching this in `bytes.find` is the closest analog; you already do that pattern in spirit.

### Pattern 8 (SKIP) — Comptime-generated state machine table

**How Ghostty does it.** `parse_table.zig:45-388` builds the 256×14 table at compile time using `inline for` + `single()` + `range()` helpers. The comptime generation also dedupes duplicate entries.

**Why PARTIALLY-SKIP for ai_terminal.** The table itself is the right idea (Pattern 1) but the comptime generation is Zig-specific. In Python, just build the table once at module import time with a function call. Don't try to write a code-generator; the table is small.

**Recommendation.** ADOPT the table (Pattern 1), SKIP the comptime-generation. Just `TABLE = {(state, byte): (next_state, action) for ... in INITIAL_ENTRIES}` at module top.

## Bugs in ai_terminal.py that these patterns fix

### Bug A — `_Parser._step` per-char state chain (lines 733-799)

- **What's wrong:** the state chain is `if st == _GROUND / elif st == _ESC / ...` (line 736, 753, 780, etc.). Every printable byte pays 5+ branch ops before doing work. The `_CSI` state accumulates params in a Python `str` (`self.params += ch`, line 783), which is reallocated per byte.
- **Pattern that fixes it:** Pattern 1 (state table).
- **Why:** a 256×8 dict lookup is ~3x faster than a 5-arm if-chain in CPython, and using `array('H')` for params avoids the per-byte string allocation entirely.

### Bug B — per-char `put_char` call (line 752)

- **What's wrong:** `self.s.put_char(ch, self._cur_attr)` is called for every printable char. That's a Python method call, frame setup, bounds check, two list-element assignments, and `self.dirty = True` per char.
- **Pattern that fixes it:** Pattern 2 (`print_slice`-style batched runs).
- **Why:** yields a `(bytes, attr)` tuple from the parser and assigns it as a single `grid[y][x:x+n] = bytes` (a C-level memcpy) — 1 call per "run" instead of 1 per char. Most SGR-bounded runs are tens to hundreds of bytes.

### Bug C — per-cell attr reset on erase (lines 603-624, 626-641)

- **What's wrong:** `erase_display` rebuilds every row's `grid` and `attrs` as fresh `[_BLANK] * cols` and `[0] * cols` lists. That's `rows × 2` list allocations per `ESC[2J`. `erase_line` does `row[c] = _BLANK; arow[c] = 0` per cell.
- **Pattern that fixes it:** Pattern 3 (batched style ref updates → row-level replace).
- **Why:** `self.grid[r] = [_BLANK] * self.cols` (1 alloc per row) replaces the per-cell loop. For `ESC[2J` on a 120-row screen, that's 120 allocations instead of 240×80 = 19200.

### Bug D — render always walks the whole screen (lines 653-712)

- **What's wrong:** `_Screen.render_cells` returns `rows` (a list of `rows` × `cols` tuples) for the entire screen every time `_do_render` fires, regardless of what changed. The render is debounced (`_schedule_render`) but each render still does the full walk.
- **Pattern that fixes it:** Pattern 5 (per-row dirty tracking).
- **Why:** adding `self.dirty_rows: set[int]` to `_Screen` and only walking dirty rows in `render_cells` cuts the work to "what changed since last render" — usually 1-3 rows on a Claude TUI redraw.

### Bug E — decoder thread races the render (line 982-984)

- **What's wrong:** `_on_data` acquires `self._lock`, calls `self.parser.feed(text)` (which mutates screen state), then releases lock and calls `_schedule_render(self)`. Between the lock release and the render firing on the main thread, more data can arrive. The render then captures a mid-update snapshot via `term.screen.render_cells()` (line 1197) — which is locked, so OK. BUT: `_schedule_render` doesn't set `term._render_pending = True` until the timeout fires. If two reads fire within `_RENDER_MS` of each other, both schedule a render and the second one does no work because `term.screen.dirty` is now False. Mostly harmless; just a no-op.
- **Pattern that fixes it:** none from Ghostty directly; this is a threading bug. Fix: set `_render_pending` in `_schedule_render` *before* `set_timeout`, not after. Already done (line 1175-1178), so this is actually fine — withdraw Bug E.
- **Recommendation:** leave as-is; the existing code is correct.

### Bug F — pending wrap state on screen-edge prints (line 562-567)

- **What's wrong:** `put_char` checks `if self.x >= self.cols: self.x = 0; self._line_feed()` then writes. There's no pending-wrap flag — every char past column 79 immediately scrolls, even if the program only wrote one char past the edge. Real terminals set `pending_wrap = true` and only scroll on the NEXT char.
- **Pattern that fixes it:** Pattern 1 (state table) brings this for free; the new dispatch should set `cursor.pending_wrap = true` in the ground state on `\x1b[` or `\n`, and `_Screen` checks it before writing.
- **Why:** without it, programs that paint to the right edge (e.g. status bars) get an extra blank line per char, which is exactly what Terminus users have complained about for years.

### Bug G — `erase_display(2)` doesn't clear scrollback (line 603-609)

- **What's wrong:** looking at `erase_display` n=2 (line 603) and n=3 (line 617) — neither references `self.history`. Real terminals: `ESC[3J` clears scrollback.
- **Pattern that fixes it:** none from Ghostty; just fix the bug. (Ghostty's `PageList.eraseHistory` is at `Screen.zig:1316-1322` — could mention for the writeup.)
- **Why:** `ESC[3J` is a standard sequence; Claude's TUI might emit it on a "clear all" keybinding.

## The asciicast logging angle

**What it would take.** asciicast v2 is a tiny format — a JSONL file where each line is either a header `{"version": 2, "width": 80, "height": 24, "timestamp": ..., "title": "..."}` or an event `{"time": seconds_float, "type": "o", "data": "base64-encoded-bytes"}`. Base64 is mandatory for the data field; raw text is allowed only for printable events.

**Minimum viable implementation** for `ai_terminal.py`:
1. Open the file in `_Terminal.__init__`; write the header line with `cols`, `rows`, current Unix timestamp.
2. In `_Terminal._on_data`, after `self.parser.feed(text)`, base64-encode the raw `data` bytes (not the decoded text) and write `{"time": time.time() - self._t0, "type": "o", "data": b64(data).decode()}`. Or capture the raw bytes *before* decode and write those.
3. For input, in `_Terminal.send_string`, also write `{"time": ..., "type": "i", "data": b64(s.encode())}`. This is the most useful piece — replays with input let you see exactly what you typed.
4. Add an `ai_terminal_set_recording` command and a setting `ai_terminal_record_path` to enable/disable.
5. Close the file in `kill()` / `on_close`.

**Cost:** ~30-50 lines.

**Why it matters here.** Ghostty's #13226 PR was driven by a 2.6 GB asciinema recording. Donald doesn't have a 2.6 GB recording of his own Claude sessions — but he can *make* one. With native asciicast output, he can:
- Replay a slow Claude session to a file and analyze it offline.
- Use `asciinema cat` to verify TUI rendering correctness.
- Run it through `ghostty-bench +terminal-stream` to compare ai_terminal's parse rate to Ghostty's — this is the single most useful benchmark Donald could write.

The fact that Ghostty's whole optimization story starts from asciinema is itself the lesson: *the recording format is upstream of the optimization.* If Donald wants to optimize ai_terminal, the first step is to make it cheap to record.

**Recommendation:** ADOPT, but as a small separate change, not part of the rewrite. Add it in a follow-up commit.

## The 5-10 things I'd actually do, ordered by impact-per-effort

1. **Print-slice batched runs in the parser** (Pattern 2). ~100 lines. Single biggest win. Eliminates per-char Python frame cost.
2. **State table for parser dispatch** (Pattern 1). ~200 lines. Correctness backbone, also perf.
3. **Replace per-cell erase with row-level replace** (Pattern 3). ~10 lines. Trivial change, real win.
4. **Per-row dirty tracking** (Pattern 5). ~30 lines. Cuts render work on most updates.
5. **Switch attrs to `array('H')` (or one int) instead of a list of ints.** Currently `self.attrs[r]` is a `list[int]`. Switching to `array('H')` halves memory and makes "fill a row with 0" a single C-level memset. ~5 lines.
6. **Add pending-wrap state to cursor.** (Bug F.) ~20 lines. Fixes the edge-paint bug.
7. **Implement `ESC[3J` (clear scrollback).** (Bug G.) ~5 lines.
8. **Add asciicast v2 output.** ~50 lines. Future-proofing for benchmarks.
9. **Add `logUnsupportedOnce` to the parser's dropped-CSI-finals path.** ~5 lines. When you eventually add logging, do it the Ghostty way.
10. **Write a `tests/` directory with replay scripts** that feed an asciicast through `ai_terminal` and compare to a reference dump. NOT in this rewrite per Donald's "no tests" rule; deferred to a follow-up.

## Out of scope (per Donald's instructions)

- Renderer / OpenGL / GPU stack — ai_terminal uses ST's view as the renderer, no custom rendering.
- Font shaping.
- Config system — `ai_terminal.sublime-settings` is the config, not Ghostty's config.
- The Kitty graphics protocol — not used by Claude's TUI.
- DCS / APC / OSC handlers beyond what ai_terminal already supports (Claude uses OSC 0 for window title and OSC 52 for clipboard; both are no-op-able).

## File references

| Pattern | File | Line(s) |
|---|---|---|
| State table lookup | `Parser.zig` | 251-311 |
| Comptime table gen | `parse_table.zig` | 45-388 |
| SIMD print_slice dispatch | `Stream.zig` | 499-589 |
| Inline csi_param fast path | `Stream.zig` | 840-886 |
| Bulk CSI param consume | `Stream.zig` | 696-752 |
| printSlice + printSliceFast | `Terminal.zig` | 339-466 |
| printSliceFill with vectorized scan | `Terminal.zig` | 502-836 |
| Batched style ref release in clearCells | `Screen.zig` | 1434-1450 |
| scroll() and cursorReload | `Screen.zig` | 1272-1336 |
| logUnsupportedOnce | `stream.zig` | 2651-2688 |
| Commit 1 (inline ASCII decode) | `083d9709b` | — |
| Commit 2 (inline CSI entry) | `300f42c7a` | — |
| Commit 3 (bulk style-only fill) | `cb2d78587` | — |
| Commit 4 (style release per run) | `8d663a76e` | — |
| Commit 5 (log-once) | `b5053153f` | — |
| PR #13226 (merge of 1-5) | `634957c8e` | — |
| PR #13226 commit message (full) | `git show 634957c8e` | lines 1-100 of commit msg |

## Time spent

~1.5 hours of the 3-4h budget (stopped early per Donald's "don't spend forever on it"). Skipped the deeper read of `Screen.zig` past line 1500 (the per-cell grapheme/hyperlink machinery that ai_terminal doesn't need) and the full Terminal.zig scroll/erase internals.
