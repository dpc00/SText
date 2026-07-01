"""Top-level plugin loader for the User package.

ST auto-loads only top-level .py as plugins. After loading a plugin module, ST scans
THAT MODULE'S OWN NAMESPACE for *Command / *EventListener subclasses and registers them
(it does not recurse into imported modules). So, like LSP.boot, this file imports every
command/listener class from the subfolder modules into its own namespace, where ST's scan
finds them. Standalone subprocess scripts (ai_search_app, ai_logger_watcher, dedup_logs)
are not imported here -- they are launched as separate processes by the modules above.
"""
from User.ai.ai_sdk import (
    AiSdkViewListener,
    AiSdkKeyInterceptor,
    AiSdkReplaceCommand,
    AiSdkFocusCommand,
    AiSdkSubmitCommand,
    AiSdkNoopCommand,
    AiSdkStopCommand,
    AiSdkClearCommand,
    AiSdkOpenHereCommand,
    AiSdkOpenInEditorCommand,
)
from User.ai.ai_hub import AiHubOpenCommand, AiHubRefreshCommand, AiHubStatusListener
from User.ai.ai_tab_manager import (
    AiTrimNowCommand,
    AiDumpBufferCommand,
    AiListSessionsCommand,
    AiSearchConversationsCommand,
    AiQuitFlaskAppsCommand,
    AiEventListener,
)
from User.ai.open_ai import (
    OpenAiHereCommand,
    OpenAiInEditorCommand,
    OpenAiTerminusInEditorCommand,
    OpenAiTerminusHereCommand,
    ClaudeGrabResponseCommand,
    ClaudeSendTabCommand,
)
from User.ai.panic_dialog import (
    PanicOpenCommand,
    PanicSendCommand,
    PanicCancelCommand,
    PanicAppendCommand,
    PanicRefreshCommand,
)
from User.ai.capture_idea import CaptureIdeaCommand, OpenIdeaInboxCommand
from User.ai.claude_code_here import ClaudeCodeHereCommand
from User.ai.clear_buffer import ClearBufferCommand
from User.logs.ai_logger import AiCaptureScrollPositionCommand
from User.launchers.pb_flask_launcher import PbFlaskLauncherCommand
from User.launchers.pb_flask_launcher_silent import PbFlaskSilentCommand
from User.launchers.ssh_panel_auto_connect import SshPanelAutoConnectCommand
from User.launchers.ccstatusline_editor import CcstatuslineEditorOpenCommand
from User.config.st_config import StConfigOpenCommand
from User.config.settings_editor import SettingsEditorOpenCommand


# -- lifecycle -----------------------------------------------------------------
# ST only calls plugin_loaded()/plugin_unloaded() on the TOP-LEVEL plugin module
# (this file). Subfolder modules' own lifecycle hooks never fire after the reorg,
# so background work started there (e.g. ai_logger's 60s screenshot capture) must
# be started here by delegation.

def plugin_loaded():
    # ST only calls plugin_loaded() on the TOP-LEVEL plugin module (this file).
    # Subfolder modules' own lifecycle hooks never fire after the reorg, so every
    # submodule that defines plugin_loaded() must be invoked here by delegation.
    import importlib
    for mod_name in _PLUGIN_LOADED_MODULES:
        try:
            importlib.import_module(mod_name).plugin_loaded()
        except Exception as e:
            print(f"loader: {mod_name}.plugin_loaded failed: {e}")


def plugin_unloaded():
    import importlib
    for mod_name in _PLUGIN_UNLOADED_MODULES:
        try:
            importlib.import_module(mod_name).plugin_unloaded()
        except Exception as e:
            print(f"loader: {mod_name}.plugin_unloaded failed: {e}")


# Every subfolder module that defines plugin_loaded(). ST only calls the
# top-level module's hook, so all of these are invoked here -- including hooks
# that only print a "loaded" line, because that console message is a legitimate
# user-visible signal that the module is alive.
_PLUGIN_LOADED_MODULES = [
    "User.logs.ai_logger",          # 60s screenshot capture + JSONL logging
    "User.ai.ai_sdk",              # no-op today; wired for parity + future work
    "User.ai.ai_tab_manager",       # prints "loaded" + ensures log dir exists
    "User.ai.panic_dialog",         # restore panic-dialog phantoms after reload
]

# Every subfolder module that defines plugin_unloaded(), so reloads/quits don't
# orphan threads, hold ports, or drop state -- and console "unloaded" lines still
# fire for users.
_PLUGIN_UNLOADED_MODULES = [
    "User.logs.ai_logger",          # flush JSONL + save state
    "User.ai.ai_sdk",              # stop AI(SDK) server + bridge
    "User.ai.ai_tab_manager",       # prints "unloaded"
    "User.config.settings_editor",  # stop HTTP server (port 57324)
    "User.config.st_config",        # stop HTTP server
]
