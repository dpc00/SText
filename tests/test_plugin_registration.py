"""Guard: every command class must be imported into PluginLoader.py.

Sublime only auto-loads top-level ``.py`` in a package and only scans *that
module's own namespace* for ``*Command`` / ``*EventListener`` subclasses. A
command defined in ``ai/ai_terminal.py`` but not imported into
``PluginLoader.py`` is therefore invisible: no palette entry, no menu item, and
a key binding that does nothing at all, with no error anywhere. That is a
silent failure with no feedback, so it gets a test.

Both files are parsed with ``ast`` rather than imported, since importing
``ai_terminal`` outside Sublime is not possible.
"""

import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# Modules whose command classes PluginLoader is expected to re-export. Standalone
# subprocess scripts are deliberately excluded: they are launched as separate
# processes, never registered as plugins.
COMMAND_MODULES = (
    "ai/ai_terminal.py",
    "ai/ai_hub.py",
    "ai/open_ai.py",
    "ai/panic_dialog.py",
    "ai/capture_idea.py",
    "ai/claude_code_here.py",
    "ai/clear_buffer.py",
    "ai/view_toggles.py",
)

# Base classes whose subclasses Sublime registers.
PLUGIN_BASES = {
    "sublime_plugin.WindowCommand",
    "sublime_plugin.TextCommand",
    "sublime_plugin.ApplicationCommand",
    "sublime_plugin.EventListener",
    "sublime_plugin.ViewEventListener",
    "sublime_plugin.TextChangeListener",
}


def _parse(rel):
    path = os.path.join(REPO, rel.replace("/", os.sep))
    with open(path, "r", encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=path)


def _base_name(node):
    if isinstance(node, ast.Attribute):
        value = node.value
        if isinstance(value, ast.Name):
            return "%s.%s" % (value.id, node.attr)
    if isinstance(node, ast.Name):
        return node.id
    return ""


def declared_commands(rel):
    """Names of plugin classes defined at module scope in *rel*."""
    return {
        node.name
        for node in _parse(rel).body
        if isinstance(node, ast.ClassDef)
        and any(_base_name(b) in PLUGIN_BASES for b in node.bases)
    }


def loader_imports():
    """Every name imported into PluginLoader.py's namespace."""
    names = set()
    for node in ast.walk(_parse("PluginLoader.py")):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def test_every_command_is_registered_by_the_loader():
    imported = loader_imports()
    missing = {}
    for rel in COMMAND_MODULES:
        gap = sorted(declared_commands(rel) - imported)
        if gap:
            missing[rel] = gap
    assert not missing, (
        "These command classes are defined but never imported into "
        "PluginLoader.py, so Sublime will not register them (no palette "
        "entry, no menu item, dead key binding):\n"
        + "\n".join("  %s: %s" % (k, ", ".join(v)) for k, v in missing.items())
    )


def test_launcher_commands_are_registered():
    """Explicit check for the launcher trio, the regression that prompted this."""
    imported = loader_imports()
    for name in (
        "AiTerminalLauncherCommand",
        "AiTerminalRecentSessionsCommand",
        "AiTerminalRefreshUsageCommand",
    ):
        assert name in imported, "%s not imported in PluginLoader.py" % name


def test_command_names_referenced_by_keymap_exist():
    """Every ai_terminal_* binding must map to a real, registered class."""
    imported = loader_imports()
    for binding in _keymap():
        command = binding.get("command", "")
        if not command.startswith("ai_terminal_"):
            continue
        # snake_case command name -> CamelCase class name.
        cls = "".join(p.title() for p in command.split("_")) + "Command"
        assert cls in imported, (
            "keymap binds %r (%s) but that class is not imported into "
            "PluginLoader.py" % (binding.get("keys"), cls)
        )


# ─── keymap shadowing ────────────────────────────────────────────────────────
#
# Terminal views bind almost every key to ai_terminal_keypress so keystrokes
# reach the agent. A launcher chord that collided with one of those would work
# in a normal file and do nothing in a terminal, which is the most confusing
# possible outcome: the feature would look broken at random.

LAUNCHER_CHORDS = {
    "ctrl+alt+n": "ai_terminal_launcher",
    "ctrl+alt+h": "ai_terminal_recent_sessions",
}


def _keymap():
    import json
    import re

    path = os.path.join(REPO, "Default.sublime-keymap")
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    # The keymap allows // comments, which json cannot parse.
    return json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.M))


def test_launcher_chords_are_bound_exactly_once():
    bindings = _keymap()
    for chord, command in LAUNCHER_CHORDS.items():
        hits = [b for b in bindings if b.get("keys") == [chord]]
        assert len(hits) == 1, "%s bound %d times: %r" % (chord, len(hits), hits)
        assert hits[0].get("command") == command


def test_launcher_chords_are_not_shadowed_by_terminal_keypass():
    """The chords must also work while a terminal view has focus."""
    passthrough = [
        b for b in _keymap() if b.get("command") == "ai_terminal_keypress"
    ]
    # Sanity check that we are actually looking at the passthrough block.
    assert len(passthrough) > 100, "expected the bulk keypress bindings"
    claimed = {k for b in passthrough for k in b.get("keys", [])}
    for chord in LAUNCHER_CHORDS:
        assert chord not in claimed, (
            "%s is also bound to ai_terminal_keypress, so it would be swallowed "
            "inside terminal views" % chord
        )
