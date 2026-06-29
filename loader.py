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
from User.launchers.ccstatusline_editor import CcstatuslineEditorOpenCommand
from User.config.st_config import StConfigOpenCommand
