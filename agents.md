# SText — Local Workspace Map & Router (Layer 2)

**Root law:** If you need the master projects map, go to the parent directory: `../agents.md`

**Operational Law:** Assume you can access anything directly. Do NOT query, list, or inspect what project folders are loaded in the Sublime Text sidebar or sidebar workspaces.

---

## 🔄 SText 4-Stage Development Pipeline
Every task in this workspace must progress sequentially through these stages:
1. **Stage 1: Design & Specs** — Review UI goals in `AI_UI.md` and define configuration parameters.
2. **Stage 2: Core Coding** — Modify the target python module inside `ai/`, `backend/`, or `launchers/`.
3. **Stage 3: Local Staging** — Copy modified modules to the active Packages User folder.
4. **Stage 4: Verification & Logging** — Check live ST console logs and verify the change in real-time.

---

## 🚦 Local Context Routing Table
Use this table to immediately lock your focus onto the relevant files. Do not scan, read, or list other files in the workspace:

| Current Objective / Task | Read/Load Files (In-Scope) | Skip/Ignore Files (Out-of-Scope) | Required MCP Tools |
| :--- | :--- | :--- | :--- |
| **SText Terminal / PTY / winpty** | `ai/ai_terminal.py`, `ai_terminal.sublime-settings` | All other `ai/` scripts, UI code | ST Console log readers |
| **Inline Chat View / SDK / Client** | `ai/ai_sdk.py`, `backend/agent_query.py` | Terminal rendering modules | TCP network monitors |
| **Tab/View Management** | `ai/ai_tab_manager.py` | Local launchers, logging scripts | Sublime eval tools |
| **Auto-Restart / Plugin Loading** | `PluginLoader.py` | UI modules, core backends | File-system deployment |
| **SSH-Panel / Network Autoconnect** | `launchers/ssh_panel_auto_connect.py` | All `ai/` files, settings | Local ping tools |
| **Inbox & Status UI Panels** | `ai/ai_hub.py`, `C:\Users\donal\ideas_inbox.md` | Core PTY backends, loaders | HTML layout engines |

---

## 📝 Key Files Directory
- `ai/ai_tab_manager.py` — Tab and view management logic.
- `ai/ai_search_app.py` — Search functionality.
- `Default.sublime-commands` — Command palette registrations.
- `AI_UI.md` — Project brief for the Sublime Text AI UI wrapper.

---

## 🎯 Active Goals
* Build the multi-pane UI (separate boxes for Ideas, Conversations, Config, and Status).
* Make `Ctrl+Alt+I` text-input box the default input method for terminal commands.
* Build the side-by-side Q&A widget (Claude's questions on the left, user answers on the right).
* Implement tone/priority signaling in the graphical panels.

---

## 🤝 Session Continuity & Baton Protocol
- **The Problem:** When Sublime Text restarts, the active AI session's memory is terminated. Re-contextualizing from scratch takes 10+ minutes and causes severe user fatigue.
- **The Protocol:**
  1. **Before any Restart or Handoff:** Always write the current task state, files in scope, and the last 3 conversation turns to `.session_baton.json` at the root of SText.
  2. **Upon Startup (Session Initialization/Pickup):** Your absolute first priority upon starting a new session is to check if `.session_baton.json` exists. If it does, load it immediately to hydrate your context, explain to the user exactly what task and state you are resuming, and then delete the baton file. This ensures instant continuation with zero wait time.
