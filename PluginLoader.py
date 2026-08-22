"""Top-level plugin loader for the User package.

ST auto-loads only top-level .py as plugins. After loading a plugin module, ST scans
THAT MODULE'S OWN NAMESPACE for *Command / *EventListener subclasses and registers them
(it does not recurse into imported modules). So, like LSP.boot, this file imports every
command/listener class from the subfolder modules into its own namespace, where ST's scan
finds them. Standalone subprocess scripts (ai_search_app, dedup_logs)
are not imported here -- they are launched as separate processes by the modules above.
"""
from User.ai.ai_hub import AiHubOpenCommand, AiHubRefreshCommand, AiHubStatusListener
from User.ai.ai_tab_manager import (
    AiListSessionsCommand,
    AiQuitFlaskAppsCommand,
)
from User.ai.open_ai import (
    ClaudeGrabResponseCommand,
)
from User.ai.panic_dialog import (
    PanicOpenCommand,
    PanicSendCommand,
    PanicCancelCommand,
    PanicAppendCommand,
    PanicRefreshCommand,
)
from User.ai.capture_idea import CaptureIdeaCommand, OpenIdeaInboxCommand
from User.ai.view_toggles import (
    AiToggleGutterCommand,
    AiToggleLineNumbersCommand,
    AiToggleFoldButtonsCommand,
)
from User.launchers.pb_flask_launcher import PbFlaskLauncherCommand
from User.launchers.pb_flask_launcher_silent import PbFlaskSilentCommand
from User.launchers.ssh_panel_auto_connect import SshPanelAutoConnectCommand


# -- lifecycle -----------------------------------------------------------------
# ST only calls plugin_loaded()/plugin_unloaded() on the TOP-LEVEL plugin module
# (this file). Subfolder modules' own lifecycle hooks never fire after the reorg,
# so they must be invoked here by delegation.
#
# The daily-log HTTP server and JSONL tailer live in STLogs (Packages/STLogs).
# Do not start them from here.


def plugin_loaded():
    import importlib

    for mod_name in _PLUGIN_LOADED_MODULES:
        try:
            importlib.import_module(mod_name).plugin_loaded()
        except Exception as e:
            print(f"PluginLoader: {mod_name}.plugin_loaded failed: {e}")


def plugin_unloaded():
    import importlib

    for mod_name in _PLUGIN_UNLOADED_MODULES:
        try:
            importlib.import_module(mod_name).plugin_unloaded()
        except Exception as e:
            print(f"PluginLoader: {mod_name}.plugin_unloaded failed: {e}")


_PLUGIN_LOADED_MODULES = [
    "User.ai.ai_tab_manager",       # prints "loaded"
    "User.ai.panic_dialog",         # restore panic-dialog phantoms after reload
]

_PLUGIN_UNLOADED_MODULES = [
    "User.ai.ai_tab_manager",       # prints "unloaded"
]


