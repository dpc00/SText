# Google Gemini CLI Bug Report: Inappropriate and Unsolicited Session Conversation Replay Event (Windows - Verified on both ConPTY and winpty)

## 📌 Issue Overview
*   **Component:** Google Gemini CLI (`gemini` executable)
*   **Operating System:** Windows (win32) / Windows Terminal, winpty, & ConPTY Emulators
*   **Severity:** High (Interrupts workflow, floods stdout with a massive, redundant data stream, and freezes developer terminal view/buffer)
*   **Observed Behavior:** During an active, multi-turn chat session, the Gemini CLI randomly and inappropriately initiates a single, complete replay of the entire conversation text from the beginning of the session. This floods the terminal stream with tens of thousands of characters of historical chat turns, forcing the developer to wait 45–120 seconds for the massive spew to reach the end of the session's work before they can type another command.
*   **PTY-Backend Independence (CRITICAL PROOF):** This syndrome has been verified to occur identically on **both** native Windows ConPTY and the winpty compatibility backend. This officially isolates the bug away from being a ConPTY rendering flaw, placing the root cause squarely within the `gemini` executable's internal session-resume or stdout-flushing logic.

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

## 🛠️ The Architectural Root Cause (Open-Source Code Analysis)

By analyzing the open-source codebase of `@google/gemini-cli` (GitHub: `google-gemini/gemini-cli`), we have isolated the exact architectural mechanisms responsible for this unsolicited full-history replay phenomenon.

The bug is a race condition and a re-render cascade between the **Configuration Hot-Reload Lifecycle**, the **Ink Terminal UI State Management**, and the **Session Rehydration** engine:

### 1. The Redraw Loop in the Slash Command Processor
*   **File:** `packages/cli/src/ui/hooks/slashCommandProcessor.ts`
*   **The Code:**
    ```typescript
    case 'load_history': {
      config?.getGeminiClient()?.setHistory(result.clientHistory);
      fullCommandContext.ui.clear();
      result.history.forEach((item, index) => {
        fullCommandContext.ui.addItem(item, index);
      });
      return { type: 'handled' };
    }
    ```
*   **The Mechanics:** In an active interactive terminal session, whenever a session rehydration or history-reload is triggered, the `load_history` handler clears the UI and sequentially calls `addItem(item, index)` for every single turn in the conversation's history. 
*   **The Consequence:** Because the CLI uses **Ink** (React for Terminals) to render components, calling `addItem` inside a synchronous `forEach` loop on a large historical array triggers an avalanche of React state updates and terminal redrawing passes. This forces the engine to compile and flush tens of thousands of ANSI escape sequences and text chunks directly to `process.stdout` in a single, high-bandwidth burst (the **C-Dump**).

### 2. The Hot-Reload Trigger Race Condition
*   **Files:** `packages/cli/src/config/config.ts` and `packages/core/src/config/config.ts`
*   **The Mechanics:** The CLI implements an `onReload` callback to support hot-reloading configurations (e.g., when `.gemini/settings.json` or `GEMINI_SYSTEM_MD` environment references change). When a window-state change, terminal resize, or file-save triggers this hot-reload, the `Config` class executes a re-hydration of workspace settings.
*   **The Bug:** Under certain platform signals on Windows (both under ConPTY and winpty), this hot-reload sequence incorrectly triggers the **Session Rehydration** loop (`GeminiClient.resumeChat` in `packages/core/src/core/client.ts` delegating to `chat.initialize()` in `packages/core/src/core/geminiChat.ts`) instead of refreshing settings silently in-memory. This forces the running interactive session to execute the verbose `load_history` redraw cascade described above, even though the session is already live, active, and fully painted on the screen.

---

## 📂 Undeniable Proof: The Asciicast Stream Evidence

To isolate the root cause, a stream-layer Asciicast v3 recording patch was implemented on the terminal emulator. This patch intercepts the raw stream bytes emitted by the `gemini` executable *before* any rendering or scroll-off logic occurs.

If the bug were a rendering artifact or emulator failure, the `.cast` stream files would remain small. Instead, the raw output stream files captured directly from the PTY backend balloon into hundreds of megabytes during these events, proving conclusively that the `gemini` process itself is vomiting the entire conversation payload back to `stdout`.

**Observed Asciicast Sizes (The "C-Dump" Phenomenon):**
*   **Normal Session File:** `~50 KB to 2 MB`
*   **`ai_2026-07-10_051437.cast`**: `318.2 MB` (318,267,405 bytes of stdout flooding)
*   **`ai_2026-07-12_025224.cast`**: `275.6 MB` (275,652,113 bytes of stdout flooding)
*   **`ai_2026-07-10_082439.cast`**: `231.5 MB` (231,567,832 bytes of stdout flooding)
*   **`ai_2026-07-11_010842.cast`**: `156.9 MB` (156,942,262 bytes of stdout flooding)
*   **`ai_2026-07-11_090032.cast`**: `92.4 MB` (92,462,822 bytes of stdout flooding)

These files are raw, timestamped proof of the stdout flooding occurring at the executable level. Google engineers may request these `.cast` files for verification; they can be replayed byte-for-byte to witness the precise moment the CLI spontaneously begins re-dumping the session context.

---

## 💡 Expected Behavior
1.  **No Unsolicited History Replays:** The Gemini CLI must *never* dump previous conversation turns to stdout during an active session unless explicitly commanded by the user (e.g., via a `/history` command).
2.  **Silent State Refreshes:** Any background context refreshes, system prompt reloads, or process-level syncs must occur completely silently in-memory without flushing the `.jsonl` history log to the stdout stream.
3.  **PTY/ConPTY Safety on Windows:** The CLI must implement a strict debounce or deduplication filter on Windows terminal size/signal changes to prevent triggering redundant redraw streams on process-level events.
