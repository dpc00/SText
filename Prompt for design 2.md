Building a Python-based terminal emulator inside Sublime Text that matches Ghostty's strict standards requires a structured blueprint. Because Sublime Text plugins run on an internal Python 3.8/3.3 interpreter, you cannot easily drop in an external binary or heavy compiled libraries without massive friction.
Instead, you must build a highly optimized pure-Python/C-extension terminal state machine that bridges to Sublime's TextListener and custom canvas or minihtml features.
Here is a master-class mega-prompt designed to instruct Claude to architect, scaffold, and implement this package.
------------------------------
## 🚀 Copy-Paste Master Prompt for Claude

You are an elite principal software engineer specializing in terminal emulators, low-level Python, and the Sublime Text Plugin API. 

We are building a revolutionary, best-in-class terminal emulator package for Sublime Text to completely replace 'Terminus'. It is inspired by Mitchell Hashimoto's Ghostty: prioritizing extreme performance, strict POSIX/VT100/VT220/xterm correctness, zero input-lag, and a flawless "feels good to use" human experience. It must be designed from day one to be bulletproof, highly popular, and fully compliant with Sublime Text Package Control submission guidelines.

Let's call the project "PhantomTerm".
### 1. Architectural BlueprintBecause Sublime Text relies on an embedded Python interpreter, we cannot easily use heavy UI frameworks. We must use a decoupled architecture:1. Core State Machine (The "Ghostty Core"): A highly optimized parser/state tracker for ANSI/VT escape sequences (handling colors, text attributes, cursor movements, and screen buffers).
2. PTY Bridge: A cross-platform asynchronous process manager (using `pty` on macOS/Linux and `winpty` or modern Windows Pseudo Console `ConPTY` via ctypes/win32 API).3. Sublime UI Layer: Translates the state machine's virtual screen buffer into Sublime Text views, utilizing custom syntax highlighting, phantom views, or region drawings for maximum rendering speed and zero layout thrashing.
### 2. Implementation Strategy & MilestonesBreak your development down into the following structured milestones. For each milestone, provide the concrete architectural logic, performance considerations, and complete, production-ready Python code. Do not use placeholders or write "todo" comments.
---#### Milestone 1: Cross-Platform Asynchronous PTY Spawner- Create a unified Python interface to spawn shell processes (`bash`, `zsh`, `powershell`, `cmd`).
- Implement native non-blocking read/write loops using Python's `selectors` module (since standard asyncio loops can conflict with Sublime's main thread loop).
- Handle the complex integration of Windows ConPTY using `ctypes` or `subprocess` overrides so Windows users get true 24-bit color and native pseudo-terminal handling, completely bypassing old `winpty` bottlenecks.- Implement explicit window resizing logic that cleanly signals SIGWINCH to the OS when the Sublime view width/height changes.
#### Milestone 2: The High-Performance Escape Sequence Parser- Build a zero-allocation, extremely fast ANSI/VT100/xterm escape sequence state machine in Python.- It must accurately parse and update a virtual grid buffer for:
  - SGR parameters (TrueColor 24-bit, 256-color, bold, dim, italic, underline, blink, reverse, strikethrough).
  - Cursor controls (CUP, CUD, CUF, CUB, CNL, CPL, CHA).
  - Erasing functions (ED, EL) and scrolling regions (DECSTBM).- Optimize this module heavily: use arrays/bytearrays instead of heavy string manipulation to prevent the Python Garbage Collector from causing UI stuttering.
#### Milestone 3: The Sublime View Buffer Synchronizer- Write the interface layer between your virtual screen buffer and the `sublime.View`.
- To avoid massive performance degradation, do not rewrite the entire view on every output character. Implement a "dirty region" tracking system that only updates altered cells/lines during a buffered frame rate tick (e.g., throttling updates to 60fps using `sublime.set_timeout`).
- Dynamically apply color scopes and text styles using Sublime's `view.add_regions()` engine with custom visual styles, or generate on-the-fly micro-syntaxes to utilize hardware-accelerated text rendering.
#### Milestone 4: Flawless Input Interception & Shortcuts- Implement a `TextCommand` and `EventListener` setup to capture *all* keyboard inputs (including Enter, Tab, Arrow keys, PageUp/Down, and Escape combos like Ctrl+C / Ctrl+D) and instantly pass their exact terminal byte equivalents down to the PTY.- Respect Sublime Text's standard keybindings while providing an immersive, lag-free native terminal input feeling.
---### 3. Package Control Compliance & Production Requirements- Structure all code cleanly across files: `main.py`, `pty_bridge.py`, `terminal_stream.py`, `sublime_ui.py`.- Ensure all Python code is compatible with Sublime's Python 3.8 environment and operates flawlessly isolated within its sandbox.- Include a robust error logging framework that degrades gracefully if OS permissions restrict PTY creation.

Begin by outputting Milestone 1. Provide the complete code, step-by-step logic explanations, and ensure it is production-ready.

------------------------------
## Pro-Tips for Managing Claude to Keep it "Ghostty-Grade":

* Prompt Incrementally: Do not ask Claude to write all four milestones in a single prompt. It will run out of context length/tokens and give you shallow, simplified code. Let it finish Milestone 1 perfectly, critique its output, and then say "Excellent. Proceed to Milestone 2 with the same depth."
* Enforce Strict Performance: Remind Claude that Python strings are immutable. For a terminal package to be popular, it cannot be slower than Terminus. Force Claude to utilize memory views (memoryview), byte structures, or pre-allocated lists to hold terminal line states.
* Refine the UI Layer: Sublime Text's biggest bottleneck for terminals is dumping huge amounts of text into a view buffer. When you reach Milestone 3, push Claude to optimize the sublime.set_timeout render loops to coalesce multiple incoming terminal packets into a single atomic write transaction (view.run_command("insert", ...)).

Would you like help generating the Package Control configuration files (like Main.sublime-menu or the .sublime-commands file) to match this architecture, or should we refine the Windows ConPTY ctypes bridge logic?

