"""Top-level plugin loader for the User package.

ST auto-loads only top-level .py as plugins. After loading a plugin module, ST scans
THAT MODULE'S OWN NAMESPACE for *Command / *EventListener subclasses and registers them
(it does not recurse into imported modules). So, like LSP.boot, this file imports every
command/listener class from the subfolder modules into its own namespace, where ST's scan
finds them. Standalone subprocess scripts (ai_search_app, ai_logger_watcher, dedup_logs)
are not imported here -- they are launched as separate processes by the modules above.
"""
import sys

# --- SText Auto-Restart on Hot-Reload ---
# If SText's PluginLoader is reloaded (which happens when you copy nested submodules
# and touch/copy PluginLoader.py), we detect the reload sentinel. To prevent
# destructive module reloading, crashing terminals, and color scheme wipes, we:
# 1) Refuse to perform any further imports or registrations.
# 2) Spawn a detached PowerShell process to restart Sublime Text.
# 3) Trigger a graceful exit of Sublime Text.
if getattr(sys, "_stext_plugin_loader_loaded", False):
    print("\n[SText] Hot-reload of PluginLoader detected! Initiating clean IDE restart...")
    try:
        import sublime
        import subprocess
        
        executable = sublime.executable_path()
        # Wait 500ms to allow ST to exit, then start a fresh instance
        restart_cmd = f'Start-Sleep -Milliseconds 500; Start-Process -FilePath "{executable}"'
        
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-Command", restart_cmd],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL
        )
        sublime.set_timeout(lambda: sublime.run_command("exit"), 50)
    except Exception as e:
        print(f"[SText] Failed to trigger auto-restart: {e}")
        
    raise ImportError("SText auto-restart triggered.")

# Mark that SText has successfully loaded for the first time in this session
sys._stext_plugin_loader_loaded = True

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
from User.ai.ai_terminal import (
    AiTerminalOpenHereCommand,
    AiTerminalOpenInEditorCommand,
    AiTerminalSelectProfileCommand,
    AiTerminalSendStringCommand,
    AiTerminalKeypressCommand,
    AiTerminalRenderCommand,
    AiTerminalNukeCommand,
    AiTerminalNoopCommand,
    AiTerminalDumpScreenCommand,
    AiTerminalViewListener,
    AiTerminalKeyInterceptor,
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
from User.ai.view_toggles import (
    AiToggleGutterCommand,
    AiToggleLineNumbersCommand,
    AiToggleFoldButtonsCommand,
)
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

def _find_system_python():
    import os
    import shutil
    for name in ("python", "python3", "py"):
        path = shutil.which(name)
        if path and os.path.isfile(path):
            return path
    return None


def _start_ai_log_server():
    import os
    import shutil
    import socket
    import subprocess

    port = 9511
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.25)
    try:
        sock.bind(("127.0.0.1", port))
        sock.close()
    except OSError:
        return

    script = os.path.join(
        os.path.dirname(__file__), "logs", "ai_log_server.py"
    )
    if not os.path.exists(script):
        print(f"PluginLoader: ai_log_server.py not found at {script}")
        return

    python_exe = _find_system_python()
    if not python_exe:
        print("PluginLoader: no system python found; ai_log_server not started")
        return

    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    try:
        from User.winutil._job import assign_pid
    except Exception:
        assign_pid = None
    proc = subprocess.Popen(
        [python_exe, script],
        creationflags=subprocess.CREATE_NO_WINDOW,
        startupinfo=si,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    if assign_pid:
        try:
            assign_pid(proc.pid)
        except Exception:
            pass
    print(f"PluginLoader: ai_log_server started (pid={proc.pid})")


def plugin_loaded():
    # ST only calls plugin_loaded() on the TOP-LEVEL plugin module (this file).
    # Subfolder modules' own lifecycle hooks never fire after the reorg, so every
    # submodule that defines plugin_loaded() must be invoked here by delegation.
    import importlib
    for mod_name in _PLUGIN_LOADED_MODULES:
        try:
            importlib.import_module(mod_name).plugin_loaded()
        except Exception as e:
            print(f"PluginLoader: {mod_name}.plugin_loaded failed: {e}")
    _start_ai_log_server()


def plugin_unloaded():
    import importlib
    for mod_name in _PLUGIN_UNLOADED_MODULES:
        try:
            importlib.import_module(mod_name).plugin_unloaded()
        except Exception as e:
            print(f"PluginLoader: {mod_name}.plugin_unloaded failed: {e}")


# Every subfolder module that defines plugin_loaded(). ST only calls the
# top-level module's hook, so all of these are invoked here -- including hooks
# that only print a "loaded" line, because that console message is a legitimate
# user-visible signal that the module is alive.
_PLUGIN_LOADED_MODULES = [
    "User.logs.ai_logger",          # 60s screenshot capture + JSONL logging
    "User.ai.ai_sdk",              # no-op today; wired for parity + future work
    "User.ai.ai_terminal",          # ConPTY Claude terminal; start resize poller
    "User.ai.ai_tab_manager",       # prints "loaded" + ensures log dir exists
    "User.ai.panic_dialog",         # restore panic-dialog phantoms after reload
]

# Every subfolder module that defines plugin_unloaded(), so reloads/quits don't
# orphan threads, hold ports, or drop state -- and console "unloaded" lines still
# fire for users.
_PLUGIN_UNLOADED_MODULES = [
    "User.logs.ai_logger",          # flush JSONL + save state
    "User.ai.ai_sdk",              # stop AI(SDK) server + bridge
    "User.ai.ai_terminal",          # kill all live ConPTY children
    "User.ai.ai_tab_manager",       # prints "unloaded"
    "User.config.settings_editor",  # stop HTTP server (port 57324)
    "User.config.st_config",        # stop HTTP server
]



