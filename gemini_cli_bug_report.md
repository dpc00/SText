# Google Gemini CLI Bug Report: Inappropriate and Unsolicited Session Conversation Replay Event (Windows / ConPTY)

## 📌 Issue Overview
*   **Component:** Google Gemini CLI (`gemini` executable)
*   **Operating System:** Windows (win32) / Windows Terminal & Custom ConPTY Emulators
*   **Severity:** High (Interrupts workflow, floods stdout with a massive, redundant data stream, and freezes developer terminal view/buffer)
*   **Observed Behavior:** During an active, multi-turn chat session, the Gemini CLI randomly and inappropriately initiates a single, complete replay of the entire conversation text from the beginning of the session. This floods the terminal stream with tens of thousands of characters of historical chat turns, forcing the developer to wait 45–120 seconds for the massive spew to reach the end of the session's work before they can type another command.

---

## 💻 Environment & Configuration Context
*   **CLI Launcher:** Spawns via a standard Windows batch wrapper (`lg.bat`) setting environment variables and executing `gemini`.
*   **Terminal Emulator:** Custom Windows-native ConPTY-based terminal emulator (`ai_terminal.py` replacing the default Terminus package inside Sublime Text).
*   **Workspace System Context:** Large local codebases, with rules preloaded via local context configurations (`C:\Users\donal\agents.md`, `C:\Users\donal\projects\SText\ai_sdk_prompt.md`).

---

## 🔍 Detailed Bug Description & Mechanics

During a standard, interactive development session inside a custom PTY terminal, the Gemini CLI suddenly—and without any user instruction, command-line flags, or scrollback inputs—begins a single, complete re-print/replay of the entire conversation log of the active session, outputting all previous turns one by one.

1.  **Stdout Flooding:** The CLI dumps the entire chronological session history (all previous `User:` and `Model:` turns) to stdout as a single, high-bandwidth stream.
2.  **Workflow Lockup:** In custom Python-based or Sublime Text-integrated terminal interfaces, parsing and rendering this sudden flood of ANSI escape sequences and historical text blocks completely bottlenecks the UI thread. The developer is held hostage for 1 to 2 minutes, watching their entire hours-long session replayed on screen.
3.  **No Approved User Action:** This replay is completely unrequested. No `--resume` or `-r` flags were added to the active process, and the user did not execute any history-restoration commands.

---

## 🛠️ Probable Root Cause Areas for Engineering Review

Based on system-level tracing and terminal logs, we urge Google's engineering team to investigate the following integration areas:

### 1. Ambiguous Signal Handling or Silent Process Re-initialization
On Windows, when a custom terminal PTY experiences a silent thread sync, a window focus change, or a terminal resize (`ResizePseudoConsole`), the subprocess environment may undergo a silent state refresh. 
*   **The Bug:** The Gemini CLI's automatic session-resume engine is incorrectly interpreting these internal signals as a session restart. It automatically reads the latest active session file from `~/.gemini/tmp/` and triggers a full history replay to stdout to "restore" the screen, despite the session already being live and active.

### 2. Automatic Context/Environment Syncing
If the `gemini` executable is configured to watch external prompt wrappers or environment variables (e.g., `GEMINI_SYSTEM_MD`), saving a file in the workspace can trigger a context refresh.
*   **The Bug:** When the CLI detects a change in the environment, it reloads the session state to apply the new system-prompt context. However, instead of performing this refresh silently in-memory, the CLI incorrectly triggers a full, verbose history replay to stdout.

### 3. Autoregressive Repetition Collapse (Model Attention Capture)
When the cumulative token count of the conversation transcript grows large or contains dense technical codeblocks, the model's internal prompt-turn formatting (`User:` / `Model:`) can cause attention capture.
*   **The Bug:** The model gets confused by its own context history and autocompletes by sequentially writing out the previous turns of the conversation verbatim in its new turn. Because the CLI lacks a stop-sequence filter (such as immediately terminating generation if the model outputs `User:`), it lets the model dump the entire chat history back to the terminal stdout.

---

## 💡 Expected Behavior
1.  **No Unsolicited History Replays:** The Gemini CLI must *never* dump previous conversation turns to stdout during an active session unless explicitly commanded by the user (e.g., via a `/history` command).
2.  **Silent State Refreshes:** Any background context refreshes, system prompt reloads, or process-level syncs must occur completely silently in-memory without flushing the `.jsonl` history log to the stdout stream.
3.  **PTY/ConPTY Safety on Windows:** The CLI must implement a strict debounce or deduplication filter on Windows terminal size/signal changes to prevent triggering redundant redraw streams on process-level events.
