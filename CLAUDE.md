Sublime Text 4 User package.

## Deployment

The git repo is at `C:\Users\donal\projects\SText\`. The live ST package is at
`C:\Users\donal\AppData\Roaming\Sublime Text\Packages\User\`.
After editing any plugin file, copy it to AppData before testing:
```
Copy-Item "C:\Users\donal\projects\SText\<file>" "$env:APPDATA\Sublime Text\Packages\User" -Force
```

## Key files

- `claude_tab_manager.py` — logs Claude Code Terminus sessions to `~/.claude/conversation_logs/`;
  also provides "Claude: List Recent Sessions" and "Claude: Search Conversations" palette commands
- `claude_search_app.py` — Flask search app (port 5758) over Claude's JSONL session files
- `dedup_logs.py` — cleans and deduplicates conversation log files; run manually when logs get messy
- `Default.sublime-commands` — command palette entries

## Claude Code session files

Claude stores its own session history as JSONL at `~/.claude/projects/*/uuid.jsonl`.
These are cleaner than the text logs for search purposes. The Flask app reads from them directly.

## Log cleaning

`dedup_logs.py` handles:
- Duplicate blocks (from plugin reloads re-flushing the buffer)
- Terminal status-bar lines (Session/Cost/Ctx/Mem/← for agents strips)
- Merged lines caused by terminal `\r` overwriting
- Writes output as LF-only (binary) to avoid Windows CRLF issues

Run it when the log looks messy. It is safe to run repeatedly.

## Claude Code auth

If Gmail or Google Drive MCP tools silently disappear:
```
claude auth logout
claude auth login
```
Then complete the browser auth flow. This fixes expired Google OAuth tokens.
Reconnecting from MCP settings or restarting Claude Code does NOT fix it.
