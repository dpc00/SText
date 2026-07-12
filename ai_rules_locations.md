# AI Agent Global Rules & Custom Instructions Reference Guide

This document acts as a comprehensive reference cataloging exactly where major development AI agents and LLM tools expect to load their **global, system-wide, and cross-project rules** on a Windows operating system. It also outlines a unified, non-redundant symbolic linking plan to synchronize all instructions to a single source of truth (`C:\Users\donal\agents.md`).

---

## 🛑 The Critical Safety Mandate (Rclone & Google Drive)

Before configuring any AI agent, this rule must be injected as their absolute highest-priority system boundary:

> **THERE MUST BE A BACKSLASH AFTER A GOOGLE DRIVE CONFIGURED STORE SPECIFIER (e.g., `GD:\data\` instead of `GD:data`).**
> *   **The Danger:** Failing to include a backslash after a Google Drive remote specifier (such as `GD:data` instead of `GD:\data\`) causes rclone to treat the target as a relative file or flat destination rather than an explicit folder. This has historically caused previous agents (like Claude) to execute destructive relative overwrite commands that wipe out synchronized directories and files on Google Drive.
> *   **The Rule:** Always, without exception, specify a backslash after the remote specifier to enforce directory safety (e.g., use `GD:\data\` or `GD:\`).
> *   **Secondary Rule:** No automated cleanups, folder pruning, or file deletions may be executed on any user folder without explicit written prompt confirmation.

---

## 📊 Phase 1: Reference Catalog (One AI at a Time)

### 1. Claude Code (Anthropic CLI)
*   **Expected Global Path:** `C:\Users\donal\.claude\CLAUDE.md` (resolves from `~/.claude/CLAUDE.md`)
*   **Alternative File:** `C:\Users\donal\.clauderules` (resolves from `~/.clauderules`)
*   **Assembly Logic:** Loaded automatically at startup as the default system prompt layer before any local repository files are parsed.

### 2. Cursor (Anysphere IDE)
*   **Expected Global Path:** `C:\Users\donal\.cursor\rules\` (resolves from `~/.cursor/rules/`)
*   **Assembly Logic:** Looks for `.mdc` or `.md` files inside this directory. Files configured inside this folder can be set to apply *Globally/Always* across all workspaces.

### 3. Cline (VS Code Extension)
*   **Expected Global Path:** `C:\Users\donal\Documents\Cline\Rules\` (resolves from `~/Documents/Cline/Rules`)
*   **Alternative Path:** `C:\Users\donal\.agents\AGENTS.md` (resolves from `~/.agents/AGENTS.md`)
*   **Assembly Logic:** Combines files from these global directories with local workspace rules (`.clinerules/`) on initialization.

### 4. Roo Code (Formerly Roo Cline VS Code Extension)
*   **Expected Global Path:** `C:\Users\donal\.roo\rules\` (resolves from `~/.roo/rules/`)
*   **Assembly Logic:** Loads `.md` rules from this user-specific rules directory across all open VS Code workspaces.

### 5. Devin (Cognition Labs CLI)
*   **Expected Global Path:** `C:\Users\donal\.agents\AGENTS.md` (resolves from `~/.agents/AGENTS.md`)
*   **Assembly Logic:** Automatically appends this tool-agnostic AGENTS file to its global initialization prompt.

### 6. Ollama & Qwen (Local Model Runner & Qwen LLM Family)
*   **Expected Global Path:** N/A (Inference runners do not passively monitor the filesystem for rules files).
*   **Assembly Logic:** Rules are compiled into a custom local model using a **`Modelfile`** with the **`SYSTEM`** instruction:
    ```dockerfile
    FROM qwen2.5
    SYSTEM """
    [Your Global Rules and Safety Admonitions]
    """
    ```
    Then run: `ollama create qwen2.5-safe -f Modelfile`. (Can also be passed at runtime using the `--system` command flag).

### 7. Codex (OpenAI CLI & Mac App)
*   **Expected Global Path:** `C:\Users\donal\.codex\AGENTS.md` (resolves from `~/.codex/AGENTS.md`)
*   **Assembly Logic:** Personalization configurations edited inside the Codex Mac app settings (Personalization -> Custom instructions) automatically read and write directly to this file on your disk.

### 8. GitHub Copilot (VS Code Extension)
*   **Expected Global Path:** `%APPDATA%\Code\User\globalStorage\github.copilot\instructions.md`
*   **Assembly Logic:** If `"github.copilot.chat.codeGeneration.useInstructionFiles"` is enabled in VS Code Settings, Copilot Chat reads global custom instructions from this JSON/Markdown storage in your app config directory. (Local project instructions can be added to `.github/copilot-instructions.md`).

### 9. Gemini (Google CLI / SText Integration)
*   **Expected Global Path:** `C:\Users\donal\.gemini\GEMINI.md` (resolves from `~/.gemini/GEMINI.md`)
*   **Assembly Logic:** Loaded on session initialization. On this system, it contains a direct redirect to read `C:\Users\donal\agents.md`.

### 10. OpenClaw (Autonomous Multi-Agent CLI)
*   **Expected Global Path:** `C:\Users\donal\.openclaw\workspace\AGENTS.md`
*   **Assembly Logic:** Builds its system prompt by composing workspace files, using this central workspace identity file to load core rules and safety boundaries.

### 11. SText (Your Custom Sublime Text plugin context)
*   **Expected Global Path:** `C:\Users\donal\agents.md`
*   **Assembly Logic:** Loaded directly as the primary starting lobby context for all SText conversations and LLM subprocess bridges.

---

## 🗺️ Phase 2: Unified Symlink Implementation Plan

To establish **one single source of truth** across all eleven AI platforms and prevent file/sync-tree destruction, we can link each tool's target rules file straight back to your SText master file (**`C:\Users\donal\agents.md`**).

Run the following native Windows symbolic link commands in an Administrator Command Prompt to activate this shield system-wide:

```cmd
:: 1. Link Claude Code
mklink "C:\Users\donal\.claude\CLAUDE.md" "C:\Users\donal\agents.md"

:: 2. Link Codex
mklink "C:\Users\donal\.codex\AGENTS.md" "C:\Users\donal\agents.md"

:: 3. Link OpenClaw
mklink "C:\Users\donal\.openclaw\workspace\AGENTS.md" "C:\Users\donal\agents.md"

:: 4. Link Cursor
mklink "C:\Users\donal\.cursor\rules\global.md" "C:\Users\donal\agents.md"

:: 5. Link Roo Code
mklink "C:\Users\donal\.roo\rules\global.md" "C:\Users\donal\agents.md"

:: 6. Link Cline & Devin standard
mklink "C:\Users\donal\.agents\AGENTS.md" "C:\Users\donal\agents.md"

:: 7. Link Gemini CLI
mklink "C:\Users\donal\.gemini\GEMINI.md" "C:\Users\donal\agents.md"
```

Once linked, editing `C:\Users\donal\agents.md` will instantly propagate your Google Drive rclone backslash boundaries to all running AI systems, preventing any future folder safety violations!
