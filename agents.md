# SText — Local Workspace Map & Router (Layer 2)

**Root law:** If you need the master projects map, go to the parent directory: `../agents.md`

**Operational Law:** Assume you can access anything directly. Do NOT query, list, or inspect what project folders are loaded in the Sublime Text sidebar or sidebar workspaces.

---

## 🔄 SText 5-Stage Development Pipeline
Every task in this workspace must progress sequentially through these stages:
1. **Stage 1: Design & Specs** — Review UI goals in `AI_UI.md` and define configuration parameters.
2. **Stage 2: Core Coding** — Modify the target python module inside `ai/`, `backend/`, or `launchers/`.
3. **Stage 3: Local Staging** — Copy modified modules to the active Packages User folder.
   - `ai/ai_terminal.py` must be staged to `Packages/User/ai/ai_terminal.py` (NOT the `Packages/User` root).
   - Before staging, verify by `grep`/`Test-Path` that no stale root copy exists; if it does, delete it.
4. **Stage 4: Verification & Logging** — Check live ST console logs and verify the change in real-time.
5. **Stage 5: Commit** — After the change is verified working, `git add` + `git commit` immediately. SText is a backup repo; uncommitted work is lost work. A verified, working change that is not committed is an incomplete task. Commit even if the user did not ask — this repo exists to snapshot work.

### ⛔ Commit Discipline (non-negotiable)
- **Commit on every verified code change** (modules, launchers, behavior-affecting settings). Not "when asked." Not "at the end of the session." After each change that compiles and works.
- **Do NOT commit personal editor preferences** (font size, color scheme tweaks, window layout). Those are the user's environment, not code. Leave them uncommitted.
- The `pybak` commits in history are the backup script's automatic snapshots. Agent commits should be **descriptive** (`feat:`, `fix:`, `refactor:`), not `pybak`, so they're distinguishable.
- Uncommitted working changes that get wiped by a later `git checkout`/`restore` are the agent's fault, not the user's. The agent that made the change owns the commit.
- Before any `git checkout <commit> -- <file>` or `git restore`, check `git status` and `git diff` first. If there are uncommitted changes that contain valuable work, **commit them or stash them before destroying them.**

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

## 🛠 Editor Authority — sublime-mcp is the primary editor interface
Default to sublime-mcp tools for everything — read, edit, save, close, find, navigation, selection, outside-workspace paths, all of it. ST has no sandbox; outside-workspace is just a path and `sublime-mcp_open_file` opens it like any other. Never use `eval_python` or `run_command` to bypass tool routing — that's the Gemini bug, not a solution. Fall back to built-in filesystem tools (read/edit/write/grep/glob) ONLY when ST or the sublime-mcp bridge is non-responsive (frozen, crashed, plugin-load failure, MCP HTTP error). The fallback trigger is ST being down, not the path being outside the workspace.

**Related rules (established this session):**
- Advisory, not prohibition — built-in tools stay available as an escape hatch for when ST is dysfunctional.
- ST has no sandbox; outside-workspace is not a special case. Gemini's sandbox was the bug, not the design.
- Never use `eval_python` to bypass sandbox (the Gemini bug).
- Never use `run_command` as a generic escape hatch — the user cannot see what the agent is doing. Every ST command should be a named, typed MCP tool with visible args.
- Expose ALL of ST's capabilities as dedicated typed tools with detailed instructions. User: "add every ST component, command that it comes with."
- sublime-mcp is faster than built-in read/edit (45x on edit, 11x on read warm) — performance is not a reason to bypass it.
- Phase B (exposing ST's built-in commands as typed MCP tools) is COMPLETE as of 2026-07-21. Status and batch breakdown live in the sublime-mcp repo's own docs (`sublime-mcp/docs/AGENT_GUIDE.md` and `sublime-mcp/docs/agents.md`), not here.

---

## 📺 Terminal Visibility Rule
When sublime-mcp is available, agents MUST prefer the visible terminal pattern
(see `sublime-mcp/docs/visible_terminal_skill.md`) over the native Bash tool for shell commands.
It runs commands in a visible ST terminal tab and captures exit codes. Fall back
to the native Bash tool only when sublime-mcp is unavailable.

---

## 🤝 Session Continuity & Baton Protocol
- **The Problem:** When Sublime Text restarts, the active AI session's memory is terminated. Re-contextualizing from scratch takes 10+ minutes and causes severe user fatigue.
- **The Protocol:**
  1. **Before any Restart or Handoff:** Always write the current task state, files in scope, and the last 3 conversation turns to `.session_baton.json` at the root of SText.
  2. **Upon Startup (Session Initialization/Pickup):** Your absolute first priority upon starting a new session is to check if `.session_baton.json` exists. If it does, load it immediately to hydrate your context, explain to the user exactly what task and state you are resuming, and then delete the baton file. This ensures instant continuation with zero wait time.
