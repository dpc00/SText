Before doing anything else, read C:\Users\donal\agents.md and follow its instructions.

---

# SText Development & Debugging Guide

## 1. Hot-Reloading Nested Submodules
Sublime Text only automatically reloads top-level `.py` files in the package root (e.g., `PluginLoader.py`). It does NOT watch or reload files in nested subdirectories (like `ai/` or `logs/`).

To solve this, `PluginLoader.py` has a nested submodule reloader at the very top:
```python
# --- Nested Submodule Reloader ---
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("User.") and mod_name != "User.PluginLoader":
        del sys.modules[mod_name]
```

### Development Workflow:
1. Edit any subdirectory `.py` file in the Git workspace `C:\Users\donal\projects\SText\` and save it.
2. Deploy/copy the modified files to the active `Packages\User\` directory (`C:\Users\donal\AppData\Roaming\Sublime Text\Packages\User\`) so Sublime Text sees the changes.
3. Touch (or copy) `PluginLoader.py` in the active `Packages\User\` directory.
4. This triggers Sublime Text to reload the active `PluginLoader.py`, which clears the submodule caches and imports the updated code fresh from disk.

---

## 2. Terminal Session Preservation on Reload
Normally, reloading `ai_terminal.py` wipes the global `_TERMINALS` registry, making all open terminal tabs completely unresponsive ("crashed") even though the background ConPTY subprocesses are still running.

`ai_terminal.py` contains a preservation block that automatically recovers active terminal sessions on reload and dynamically binds them to the new class definitions:
```python
# Preserve existing terminals on module reload so open terminal views don't "crash" (become unresponsive).
import sys as _sys
if "User.ai.ai_terminal" in _sys.modules:
    try:
        _old_mod = _sys.modules["User.ai.ai_terminal"]
        _old_terms = getattr(_old_mod, "_TERMINALS", {})
        for _vid, _term in _old_terms.items():
            _term.__class__ = _Terminal
            if hasattr(_term, "process") and _term.process is not None:
                _term.process.__class__ = _ProcessProxy
            _TERMINALS[_vid] = _term
        if _TERMINALS:
            print(f"[ai_terminal] Successfully recovered {len(_TERMINALS)} active terminal(s) on module reload.")
    except Exception as _re_err:
        print(f"[ai_terminal] Failed to recover active terminals on reload: {_re_err}")
```
Because of this, you can hot-reload the terminal code seamlessly without losing any active Claude or terminal shells.

---

## 3. Essential Debugging Commands (Tactic 2)
If you need to investigate SText runtime behavior, open the ST Console (`Ctrl+``) and run:
*   `sublime.log_commands(True)`: Logs all triggered commands and their arguments to the console.
*   `sublime.log_input(True)`: Logs all raw keystrokes received by ST (useful for debugging keybinding overlaps).

---

## 4. Automatic Package Reloader (Tactic 3)
For fully hands-free reloading of nested submodules whenever any file is saved, install the community package **AutomaticPackageReloader**:
1. Install `AutomaticPackageReloader` via Package Control.
2. Open the Command Palette and run `Automatic Package Reloader: Toggle Reload On Save`.
3. Saving any subdirectory `.py` file **inside the active `Packages\User\` directory** (or copying/deploying the edited file there) will now automatically trigger a reload of the entire package (which SText handles safely thanks to the terminal preservation logic).

---

## 5. Remote Debugging & IDE Attachment (Tactic 4)
Because standard debuggers like `pdb` will freeze the main thread and lock up Sublime Text, you should use a socket-based remote debugger.

### Telnet Debugging with `rpdb`:
1. Drop `rpdb` in your system python or your `Packages/User` directory.
2. Insert this breakpoint in SText code:
   ```python
   import rpdb
   rpdb.set_trace()
   ```
3. When hit, Sublime's thread will pause. Connect via your system terminal:
   ```bash
   telnet 127.0.0.1 4444
   ```
4. You can now step through code and inspect variables interactively from your terminal.

### IDE-based Graphical Debugging:
*   **PyCharm:** Use `pydevd_pycharm`'s remote debugging server and add `pydevd_pycharm.settrace('localhost', port=...)` inside `PluginLoader.py`.
*   **VS Code / Python Debugger:** Use `debugpy` to configure and attach to Sublime Text's `plugin_host.exe` process.

---

## 6. Live REPL & Dynamic State Exploration (The Agility Loop)
While step-by-step graphical debuggers can pause execution, they are often too slow and clunky for multi-threaded, asynchronous, or event-driven plugin architectures. The most agile alternative is a **live REPL and interactive patching loop** via the Sublime Text Python console (`Ctrl+``).

### Tactic 1: Dynamic State Inspection
Since Sublime's console is a live Python REPL running inside the editor's process, you can directly inspect global variables and registries on the fly:
*   **Inspect active terminal instances:**
    ```python
    >>> import sys; sys.modules["User.ai.ai_terminal"]._TERMINALS
    ```
*   **Query active view details:**
    ```python
    >>> v = window.active_view(); v.size(); v.viewport_extent()
    ```

### Tactic 2: Live Logic Testing
Isolate and test core internal functions by invoking them directly from the console with mocked or live objects:
```python
>>> import sys
>>> ai_term = sys.modules["User.ai.ai_terminal"]
>>> ai_term._measure(window.active_view())
```

### Tactic 3: Event-Driven Log Tracing
Rather than manually stepping through code, place high-level structured logs (such as the JSONL logging in `logs/` or `data/logs/`) to track the exact sequence of asynchronous events. This lets you trace thread transitions, PTY input/output, and lifecycle events concurrently in real-time.

---

## 7. Repository Scope & Divergent Modules (CRITICAL FOR AI AGENTS)
This repository is a cumulative backup of the active Sublime Text `User` package. Over time, it has burgueoned and diverged into multiple standalone features, experimental scripts, and half-done projects. 

### Avoid Confusion:
- **Experimental Files:** Do NOT assume every module or subdirectory file is fully integrated or active.
- **AI(SDK) Module:** Files under `ai/ai_sdk.py`, etc., represent a partially-completed (half-done) Claude CLI replacement. They are separate from the core SText user-facing plugin tab and view manager loop.
- **Guidance for AI Assistants:** Prioritize the authoritative instructions in `AGENTS.md`. Do not read out of half-done module files or assume they are applicable/involved in the core SText functionality unless specifically instructed by the user. Keep focus strictly within the requested task scope.

---

## 8. Live Command Class Hot-Swapping (Tactic 5)
### The Problem:
Even if a nested subdirectory file is successfully reloaded in Python's `sys.modules`, and even if the top-level `PluginLoader.py` is re-executed, Sublime Text's internal C++ core command registries (`sublime_plugin.window_command_classes` and `sublime_plugin.all_command_classes`) **still hold onto the old command class definitions from the initial startup scan**. Running the command in the editor will execute the stale in-memory bytecode, referencing older closure variables and stale helper functions, throwing unexpected crashes.

### The Solution:
You can programmatically hot-swap the command classes inside Sublime Text's live registries in real-time using the ST Python Console (`Ctrl+``):
```python
import sys, importlib, sublime_plugin

# 1. Force-reload the nested module
importlib.reload(sys.modules["User.ai.panic_dialog"])
import User.ai.panic_dialog

# 2. Map new command class definitions
new_classes = {
    "PanicOpenCommand": User.ai.panic_dialog.PanicOpenCommand,
    "PanicSendCommand": User.ai.panic_dialog.PanicSendCommand,
}

# 3. Hot-swap in window command classes
sublime_plugin.window_command_classes = [
    new_classes.get(cls.__name__, cls) for cls in sublime_plugin.window_command_classes
]

# 4. Hot-swap in all command lists (Application, Window, Text)
sublime_plugin.all_command_classes = [
    [new_classes.get(cls.__name__, cls) for cls in sublist]
    for sublist in sublime_plugin.all_command_classes
]
```
Once executed, the live command mappings in SText are immediately updated to the fresh bytecode on disk without needing to restart the editor.

---

## 9. Port-Locks and Background Processes
SText launches independent background daemon servers (like `ai_log_server.py` on port `9511`) by checking if the port is free before spawning them. 
### The Problem:
If SText is reloaded, or if you close and reopen SText, the old background daemon process might still be running and holding onto the port. Sublime Text's `PluginLoader.py` will see the port is bound and **will silently abort starting the new server**, leaving you running the older, stale, or crashed background code forever.

### The Solution:
Whenever you deploy updates to `ai_log_server.py` or another background script, you must explicitly kill the old running process to free the socket:
1. Find the PID of the Python process listening on port `9511` (or get it from Sublime Text's startup console logs).
2. Kill the process:
   ```powershell
   Stop-Process -Id <pid> -Force
   ```
3. Touch/copy `PluginLoader.py` to trigger a reload. It will see the port is free and immediately launch the newly updated server code.

---

## 10. Traceback vs. Bytecode Line Caching
### The Phenomenon:
You may sometimes see tracebacks in the console that print lines of code that make no semantic sense (e.g., throwing `AttributeError: 'list' object has no attribute 'strip'` on `if isinstance(content, str):`).

### The Explanation:
Python's traceback formatter reads lines **live from disk** using the standard `linecache` module, but the execution in-memory is still running the **old cached bytecode** from before the reload. If the files on disk have shifted line positions, the printed traceback line will be completely wrong.
*   **Actionable Advice:** Trust the *exception type* and *variables* over the printed line text if hot-reloads were triggered. Call `linecache.clearcache()` or restart SText to sync the traceback formatter's cache with disk.




