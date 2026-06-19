# SText — Agent Context

**Root law:** If you need the org map, go to C:\Users\donal\router.md
**If this isn't the right place for your task, go back to:** C:\Users\donal\agents.md

---

## What This Project Is
Sublime Text plugin + Claude Code UI integration. The goal is a zero-fatigue graphical wrapper around Claude Code and MCP — separate panes for chat, config, status, and ideas.

## Critical Assumption
Plugin edits in `C:\Users\donal\projects\SText\` are NOT live in Sublime Text until copied to `C:\Users\donal\AppData\Roaming\Sublime Text\Packages\User\`. Never assume a plugin behaves correctly or test results are valid until the file has been deployed there. If something isn't working, check deployment before investigating the code.

## Key Files
- ai_tab_manager.py — Tab/view management
- ai_search_app.py — Search functionality
- Default.sublime-commands — Command palette entries (may need entries merged from sublime-mcp)
- AI_UI.md — Project brief for the Sublime Text AI UI plugin

## Active Goals
- Build the multi-pane UI (Ideas / Conversation / Config / Status panes)
- ctrl-alt-i textbox as default input method
- Side-by-side Q&A widget (Claude questions left, user answers right)
- Tone/priority signaling in the UI

## Known Issues
- Default.sublime-commands may have entries that belong in sublime-mcp's MCP Commander.sublime-commands
- See C:\Users\donal\ideas_inbox.md for full list

## Related Project
sublime-mcp at C:\Users\donal\projects\sublime-mcp — the MCP server this plugin talks to
