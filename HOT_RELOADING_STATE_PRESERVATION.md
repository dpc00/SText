# Dynamic Hot-Reloading & State Preservation in Python Editor Plugins

A lightweight, pure-Python architecture for hot-reloading nested submodules and preserving active, stateful objects (threads, PTY processes, sockets, registries) across reloads without disconnecting sessions or interrupting runtime behavior.

---

## The Problem: Reloading Destroys State

In highly interactive desktop environments (like Sublime Text, VS Code, or custom Python-based GUI apps), writing plugin logic involves a constant edit-save-test loop.

Standard Python reloading (`importlib.reload`) or editor-level plugin reloaders have two major pitfalls:
1. **Shallow Reloading:** They only watch and reload top-level modules. Changes to nested submodules (e.g., `pkg/submodule.py`) are ignored because Python caches them in `sys.modules`.
2. **State Destruction:** Clearing the module cache or re-importing code completely wipes out global registries, resets state variables, and detaches any long-running processes (like terminal ConPTY children, server sockets, or active UI tabs), making existing components unresponsive or "crashed."

---

## The SText Solution

This architecture combines three pure-Python techniques to achieve **zero-downtime hot-reloading**:
1. **Recursive Submodule Cache Purging:** Forcing Python to reload nested submodules by surgically removing them from `sys.modules`.
2. **Persistent Side-Channel State Stashing:** Storing references to active runtime instances in a persistent namespace that is guaranteed *never* to be garbage-collected or reloaded (e.g., a custom property on the built-in `sys` module).
3. **Dynamic Class Hot-Swapping (`__class__` mutation):** Updating the active, live instances in-place to point to the newly reloaded class definitions, seamlessly swapping out methods and attributes without disrupting active sockets, threads, or file descriptors.

---

## 1. Surgical Cache Purging (Top-Level Reloader)

Sublime Text (and other plugin hosts) automatically reloads top-level `.py` files in the package root. However, they do not watch or reload nested subdirectory files. 

To solve this, we place a surgical cache purger at the very top of our entry-point loader (`PluginLoader.py`). Before any imports are evaluated, it scans `sys.modules`, preserves old states, and removes cached references to nested submodules so they are imported fresh from disk.

### Implementation (`PluginLoader.py`)

```python
import sys

# --- Surgical Submodule Reloader ---
# 1. Capture and preserve the old module objects before deleting them.
#    This side-channel ensures our submodules can access their previous state during reload.
sys._stext_old_modules = {}

for mod_name in list(sys.modules.keys()):
    # Filter for submodules inside your plugin package (e.g. starting with "User.")
    if mod_name.startswith("User.") and mod_name != "User.PluginLoader":
        sys._stext_old_modules[mod_name] = sys.modules[mod_name]
        del sys.modules[mod_name]

# 2. Now perform the imports. Python will be forced to read these fresh from disk.
from User.ai.ai_terminal import AiTerminalOpenCommand, _Terminal, _Screen
# ... other imports ...

# 3. Clean up the temporary state-stash after reloading is finished to avoid memory leaks.
if hasattr(sys, "_stext_old_modules"):
    try:
        del sys._stext_old_modules
    except Exception:
        pass
```

---

## 2. In-Place State Recovery & Class Hot-Swapping

When the submodule (e.g., `ai_terminal.py`) is executed fresh during the reload, it defines its classes and registers global variables. 

At the module level, we run our state recovery sequence. We look up our old self inside the persistent side-channel `sys._stext_old_modules`, extract the active instance registry, and perform an in-place hot-swap of their classes using Python’s dynamic `__class__` assignment.

### Implementation (`ai_terminal.py`)

```python
import sys as _sys

# Define the live registries and classes for the newly reloaded module
_TERMINALS = {}

class _Screen:
    def __init__(self, cols, rows):
        self.cols = cols
        self.rows = rows
        # ... complex state ...

class _Terminal:
    def __init__(self, view, pty, screen):
        self.view = view
        self.pty = pty
        self.screen = screen
        # ... live background threads/processes ...


# --- In-Place State Recovery Block ---
# This block runs at module-evaluation time during the reload process.
_old_mod = None
if hasattr(_sys, "_stext_old_modules") and "User.ai.ai_terminal" in _sys._stext_old_modules:
    _old_mod = _sys._stext_old_modules["User.ai.ai_terminal"]

if _old_mod is not None:
    try:
        # Retrieve the registry containing active sessions from the old module
        _old_terms = getattr(_old_mod, "_TERMINALS", {})
        
        for _vid, _term in _old_terms.items():
            # surgically mutate the active instance classes to point to the new classes
            _term.__class__ = _Terminal
            
            # Recurse into nested sub-components (if any)
            if hasattr(_term, "screen") and _term.screen is not None:
                _term.screen.__class__ = _Screen
                
            # Re-register the hot-swapped instance into the new module's registry
            _TERMINALS[_vid] = _term
            
        if _TERMINALS:
            print(f"[ai_terminal] Successfully recovered {len(_TERMINALS)} active terminal(s) on module reload.")
    except Exception as _re_err:
        print(f"[ai_terminal] Failed to recover active terminals on reload: {_re_err}")
```

---

## How It Works Under the Hood

### The Magic of `__class__` Hot-Swapping
In Python, user-defined class instances are highly dynamic. Assigning an object's `__class__` dynamically updates its type pointer in the Python interpreter:
```python
_term.__class__ = _Terminal
```
* **What changes:** All method lookups (e.g. `_term.render()`), property descriptors, class attributes, and parent classes are instantly redirected to the newly defined class definitions.
* **What is preserved:** The instance’s identity (its memory address/ID), its local variable store (`_term.__dict__`), active open file descriptors, sockets, running background threads, and locks remain entirely untouched.

This allows background threads and PTY processes to continue writing data directly into the active instance. The next time the main thread invokes any method on that instance, it runs the updated code fresh from disk.

---

## Benefits & Use Cases

* **Zero-Downtime Development:** You can edit terminal renderers, styling rules, keypress handlers, and log parsers, then press Save. The entire package reloads in milliseconds, and your active terminal session stays alive and interactive.
* **Thread/Socket Preservation:** Long-running web socket connections, local subprocesses, and network pools are preserved across saves.
* **Pure Python:** Works out-of-the-box on standard Python 3.x with zero binary dependencies, C-extensions, or third-party wrappers.

---

## Crucial Pitfalls & Best Practices

1. **Avoid Destructive `plugin_unloaded()` Hooks:**
   When reloading, plugin loaders typically trigger tear-down hooks (such as `plugin_unloaded()` in Sublime Text). Ensure that these hooks do *not* aggressively kill active processes or clear files if you intend to preserve them.
   ```python
   def plugin_unloaded():
       # Deliberately do NOT kill child processes here during development reloads.
       # They will naturally clean up when the parent process/editor exits.
       pass
   ```
2. **Prevent Write Storms on Settings Reload:**
   If you have settings listeners that trigger logic changes (like recalculating buffer boundaries), debounce/throttle those events so they don't fire multiple times during rapid, back-to-back submodule imports.
3. **Clean Up Your Side-Channel:**
   Always delete the side-channel attribute (`sys._stext_old_modules`) at the end of your loader sequence. If left dangling, it will keep old module references alive in memory indefinitely, causing a slow memory leak.
