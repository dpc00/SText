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


# Daily-log HTTP server (hooks → ~/data/logs/<date>.md). Must survive package
# reloads and brief process death — without it, agent hooks spool and the ST log
# tab looks "aborted" mid-session.
_AI_LOG_PORT = 9511
# Keep the gap short: when the server dies, hooks spool and the daily log
# looks "aborted". 5s is enough to avoid thrash; 30s left multi-turn holes.
_AI_LOG_KEEPALIVE_MS = 5_000
_ai_log_keepalive_scheduled = False


def _ai_log_port_free(port=None):
    import socket

    port = _AI_LOG_PORT if port is None else port
    # Probe the service instead of trying to bind the port. On Windows, a
    # wildcard listener (0.0.0.0) and a loopback bind can coexist under some
    # socket reuse combinations, so bind-based detection spawned a new logger
    # every keepalive tick even though the existing receiver was healthy.
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return False
    except OSError:
        return True


def _start_ai_log_server():
    import os
    import subprocess

    if not _ai_log_port_free():
        return False

    script = os.path.join(
        os.path.dirname(__file__), "logs", "ai_log_server.py"
    )
    if not os.path.exists(script):
        print(f"PluginLoader: ai_log_server.py not found at {script}")
        return False

    python_exe = _find_system_python()
    if not python_exe:
        print("PluginLoader: no system python found; ai_log_server not started")
        return False

    si = None
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
    # Do NOT assign_pid into the ST job: package reloads / job teardown kill
    # job members and abort the daily log. This process is intentionally
    # independent; keepalive restarts it if it dies while ST is open.
    #
    # Windows flags (best-effort stack; strip until CreateProcess accepts):
    #   CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    #   | DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    flag_attempts = [
        CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB,
        CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB,
        CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
        CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        CREATE_NO_WINDOW,
    ]
    # Server rebinds its own stderr/stdout to DIAG_DIR files; DEVNULL here only
    # prevents a console window / inherited pipe from the ST process.
    last_err = None
    proc = None
    for flags in flag_attempts:
        try:
            proc = subprocess.Popen(
                [python_exe, script],
                creationflags=flags if sys.platform == "win32" else 0,
                startupinfo=si if sys.platform == "win32" else None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
            break
        except OSError as e:
            last_err = e
            continue
    if proc is None:
        print(f"PluginLoader: ai_log_server spawn failed: {last_err}")
        return False
    print(f"PluginLoader: ai_log_server started (pid={proc.pid})")
    return True


def _ai_log_keepalive_tick():
    """If :9511 is free, restart the log server (hooks fail-open into spool otherwise)."""
    global _ai_log_keepalive_scheduled
    try:
        if _ai_log_port_free():
            if _start_ai_log_server():
                print("PluginLoader: ai_log_server restarted by keepalive")
    except Exception as e:
        print(f"PluginLoader: ai_log keepalive error: {e}")
    try:
        import sublime

        sublime.set_timeout(_ai_log_keepalive_tick, _AI_LOG_KEEPALIVE_MS)
        _ai_log_keepalive_scheduled = True
    except Exception:
        _ai_log_keepalive_scheduled = False


def plugin_loaded():
    # ST only calls plugin_loaded() on the TOP-LEVEL plugin module (this file).
    # Subfolder modules' own lifecycle hooks never fire after the reorg, so every
    # submodule that defines plugin_loaded() must be invoked here by delegation.
    import importlib
    import sublime

    for mod_name in _PLUGIN_LOADED_MODULES:
        try:
            importlib.import_module(mod_name).plugin_loaded()
        except Exception as e:
            print(f"PluginLoader: {mod_name}.plugin_loaded failed: {e}")
    _start_ai_log_server()
    # Arm keepalive once per process; reloads re-enter plugin_loaded and re-arm.
    global _ai_log_keepalive_scheduled
    if not _ai_log_keepalive_scheduled:
        sublime.set_timeout(_ai_log_keepalive_tick, _AI_LOG_KEEPALIVE_MS)
        _ai_log_keepalive_scheduled = True


def plugin_unloaded():
    import importlib

    global _ai_log_keepalive_scheduled
    _ai_log_keepalive_scheduled = False
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
    "User.ai.ai_tab_manager",       # prints "loaded" + ensures log dir exists
    "User.ai.panic_dialog",         # restore panic-dialog phantoms after reload
]

# Every subfolder module that defines plugin_unloaded(), so reloads/quits don't
# orphan threads, hold ports, or drop state -- and console "unloaded" lines still
# fire for users.
_PLUGIN_UNLOADED_MODULES = [
    "User.logs.ai_logger",          # flush JSONL + save state
    "User.ai.ai_tab_manager",       # prints "unloaded"
]


