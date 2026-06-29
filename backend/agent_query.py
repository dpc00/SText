"""agent_query.py — Stream a Claude response to stdout via Agent SDK.

Usage: python agent_query.py "your prompt here"
Each text chunk is printed and flushed as it arrives.
"""

import asyncio
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_PYTHON = r"C:\Users\donal\AppData\Local\Programs\Python\Python312\python.exe"


def _load_mcp_servers():
    """Load mcpServers from ~/.claude.json (global + all project-level) and add st-plugin."""
    servers = {}
    try:
        cfg = json.load(open(os.path.expanduser("~/.claude.json"), encoding="utf-8"))
        servers.update(cfg.get("mcpServers", {}))
        for proj in cfg.get("projects", {}).values():
            servers.update(proj.get("mcpServers", {}))
    except Exception:
        pass
    servers["st-plugin"] = {
        "type": "stdio",
        "command": _PYTHON,
        "args": [os.path.join(os.path.dirname(os.path.abspath(__file__)), "st_mcp_bridge.py")],
    }
    return servers


_MCP = _load_mcp_servers()

_ALLOWED_TOOLS = [
    # --- built-in CLI tools (commented out = blocked) ---
    "Bash",
    # "Edit",
    "Glob",
    "Grep",
    "LS",
    "Read",
    # "Write",
    "WebFetch",
    "WebSearch",
    # "NotebookEdit",
    "TodoRead",
    # "TodoWrite",   # writes files
    # --- st-plugin (socket bridge to ai_sdk.py) ---
    "mcp__st-plugin__get_window_summary",
    # "mcp__st-plugin__sublime_eval",  # executes arbitrary Python
    # --- sublime-mcp: READ-ONLY only ---
    # "mcp__sublime-mcp__add_folder",          # modifies project
    # "mcp__sublime-mcp__close_file",          # modifies ST state
    # "mcp__sublime-mcp__duplicate_line",      # modifies buffer
    "mcp__sublime-mcp__eval_python",  # executes arbitrary Python
    "mcp__sublime-mcp__eval_python_latest",  # executes arbitrary Python
    "mcp__sublime-mcp__find_in_file",
    "mcp__sublime-mcp__find_in_files",
    "mcp__sublime-mcp__focus_group",
    "mcp__sublime-mcp__fold_lines",
    "mcp__sublime-mcp__get_active_file",
    "mcp__sublime-mcp__get_active_panel",
    "mcp__sublime-mcp__get_bookmarks",
    "mcp__sublime-mcp__get_command_palette",
    "mcp__sublime-mcp__get_commands",
    "mcp__sublime-mcp__get_console_full",
    "mcp__sublime-mcp__get_console_log",
    "mcp__sublime-mcp__get_console_win",
    "mcp__sublime-mcp__get_cursor_context",
    "mcp__sublime-mcp__get_encoding",
    "mcp__sublime-mcp__get_file_content",
    "mcp__sublime-mcp__get_layout",
    "mcp__sublime-mcp__get_line_count",
    "mcp__sublime-mcp__get_menu_items",
    "mcp__sublime-mcp__get_open_files",
    "mcp__sublime-mcp__get_output_panel",
    "mcp__sublime-mcp__get_package_mcp_info",
    "mcp__sublime-mcp__get_project_data",
    "mcp__sublime-mcp__get_project_folders",
    "mcp__sublime-mcp__get_scope_at_cursor",
    "mcp__sublime-mcp__get_selection",
    "mcp__sublime-mcp__get_setting",
    "mcp__sublime-mcp__get_sheet_content",
    "mcp__sublime-mcp__get_sheets",
    "mcp__sublime-mcp__get_symbols",
    "mcp__sublime-mcp__get_syntaxes",
    "mcp__sublime-mcp__get_variables",
    "mcp__sublime-mcp__get_view_chars",
    "mcp__sublime-mcp__get_view_content",
    "mcp__sublime-mcp__get_view_phantoms",
    "mcp__sublime-mcp__get_view_size",
    "mcp__sublime-mcp__get_word_at_cursor",
    "mcp__sublime-mcp__goto_line",
    # "mcp__sublime-mcp__insert_snippet",      # modifies buffer
    # "mcp__sublime-mcp__install_package",     # installs packages
    "mcp__sublime-mcp__lookup_symbol",
    "mcp__sublime-mcp__open_control_panel",
    "mcp__sublime-mcp__open_file",
    # "mcp__sublime-mcp__redo",                # modifies buffer
    # "mcp__sublime-mcp__remove_folder",       # modifies project
    # "mcp__sublime-mcp__replace_lines",       # modifies buffer
    # "mcp__sublime-mcp__replace_selection",   # modifies buffer
    # "mcp__sublime-mcp__revert_file",         # modifies buffer
    # "mcp__sublime-mcp__run_build",           # runs build system
    # "mcp__sublime-mcp__run_command",         # runs arbitrary ST command
    # "mcp__sublime-mcp__save_all",            # writes files
    # "mcp__sublime-mcp__save_file",           # writes files
    "mcp__sublime-mcp__search_packages",
    "mcp__sublime-mcp__select_lines",
    # "mcp__sublime-mcp__send_to_view",        # modifies buffer
    # "mcp__sublime-mcp__set_encoding",        # modifies file
    # "mcp__sublime-mcp__set_layout",          # modifies ST layout
    # "mcp__sublime-mcp__set_setting",         # modifies ST settings
    "mcp__sublime-mcp__set_status",
    # "mcp__sublime-mcp__set_syntax",          # modifies file metadata
    "mcp__sublime-mcp__show_panel",
    # "mcp__sublime-mcp__sort_lines",          # modifies buffer
    # "mcp__sublime-mcp__str_replace_based_edit_tool",  # edits files
    # "mcp__sublime-mcp__toggle_comment",      # modifies buffer
    "mcp__sublime-mcp__toggle_sidebar",
    # "mcp__sublime-mcp__undo",               # modifies buffer
    # --- screenshot ---
    "mcp__screenshot__list_displays",
    "mcp__screenshot__list_windows",
    "mcp__screenshot__screenshot_region",
    "mcp__screenshot__screenshot_screen",
    "mcp__screenshot__screenshot_window",
    # --- firecrawl ---
    "mcp__firecrawl__firecrawl_agent",
    "mcp__firecrawl__firecrawl_agent_status",
    "mcp__firecrawl__firecrawl_check_crawl_status",
    "mcp__firecrawl__firecrawl_crawl",
    "mcp__firecrawl__firecrawl_extract",
    "mcp__firecrawl__firecrawl_feedback",
    "mcp__firecrawl__firecrawl_interact",
    "mcp__firecrawl__firecrawl_interact_stop",
    "mcp__firecrawl__firecrawl_map",
    "mcp__firecrawl__firecrawl_monitor_check",
    "mcp__firecrawl__firecrawl_monitor_checks",
    "mcp__firecrawl__firecrawl_monitor_create",
    "mcp__firecrawl__firecrawl_monitor_delete",
    "mcp__firecrawl__firecrawl_monitor_get",
    "mcp__firecrawl__firecrawl_monitor_list",
    "mcp__firecrawl__firecrawl_monitor_run",
    "mcp__firecrawl__firecrawl_monitor_update",
    "mcp__firecrawl__firecrawl_parse",
    "mcp__firecrawl__firecrawl_research_inspect_paper",
    "mcp__firecrawl__firecrawl_research_read_paper",
    "mcp__firecrawl__firecrawl_research_related_papers",
    "mcp__firecrawl__firecrawl_research_search_github",
    "mcp__firecrawl__firecrawl_research_search_papers",
    "mcp__firecrawl__firecrawl_scrape",
    "mcp__firecrawl__firecrawl_search",
    "mcp__firecrawl__firecrawl_search_feedback",
    # --- claude-in-chrome (no MCP server configured yet) ---
    # "mcp__claude-in-chrome__browser_batch",
    # "mcp__claude-in-chrome__computer",
    # "mcp__claude-in-chrome__file_upload",
    # "mcp__claude-in-chrome__find",
    # "mcp__claude-in-chrome__form_input",
    # "mcp__claude-in-chrome__get_page_text",
    # "mcp__claude-in-chrome__gif_creator",
    # "mcp__claude-in-chrome__javascript_tool",
    # "mcp__claude-in-chrome__list_connected_browsers",
    # "mcp__claude-in-chrome__navigate",
    # "mcp__claude-in-chrome__read_console_messages",
    # "mcp__claude-in-chrome__read_network_requests",
    # "mcp__claude-in-chrome__read_page",
    # "mcp__claude-in-chrome__resize_window",
    # "mcp__claude-in-chrome__select_browser",
    # "mcp__claude-in-chrome__shortcuts_execute",
    # "mcp__claude-in-chrome__shortcuts_list",
    # "mcp__claude-in-chrome__switch_browser",
    # "mcp__claude-in-chrome__tabs_close_mcp",
    # "mcp__claude-in-chrome__tabs_context_mcp",
    # "mcp__claude-in-chrome__tabs_create_mcp",
    # "mcp__claude-in-chrome__upload_image",
    # --- Gmail (no MCP server configured yet) ---
    # "mcp__claude_ai_Gmail__create_draft",
    # "mcp__claude_ai_Gmail__create_label",
    # "mcp__claude_ai_Gmail__delete_label",
    # "mcp__claude_ai_Gmail__get_thread",
    # "mcp__claude_ai_Gmail__label_message",
    # "mcp__claude_ai_Gmail__label_thread",
    # "mcp__claude_ai_Gmail__list_drafts",
    # "mcp__claude_ai_Gmail__list_labels",
    # "mcp__claude_ai_Gmail__search_threads",
    # "mcp__claude_ai_Gmail__unlabel_message",
    # "mcp__claude_ai_Gmail__unlabel_thread",
    # "mcp__claude_ai_Gmail__update_label",
    # --- Google Drive (no MCP server configured yet) ---
    # "mcp__claude_ai_Google_Drive__copy_file",
    # "mcp__claude_ai_Google_Drive__create_file",
    # "mcp__claude_ai_Google_Drive__download_file_content",
    # "mcp__claude_ai_Google_Drive__get_file_metadata",
    # "mcp__claude_ai_Google_Drive__get_file_permissions",
    # "mcp__claude_ai_Google_Drive__list_recent_files",
    # "mcp__claude_ai_Google_Drive__read_file_content",
    # "mcp__claude_ai_Google_Drive__search_files",
    # --- TubeAlfred (no MCP server configured yet) ---
    # "mcp__claude_ai_TubeAlfred__authenticate",
    # "mcp__claude_ai_TubeAlfred__complete_authentication",
]

_CONTEXT_FILES = [
    r"C:\Users\donal\agents.md",
    r"C:\Users\donal\router.md",
    r"C:\Users\donal\projects\SText\CLAUDE.md",
]


def _write_context_file():
    parts = []
    for path in _CONTEXT_FILES:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                parts.append(f"# {os.path.basename(path)}\n{f.read()}")
        except OSError:
            pass
    content = "\n\n---\n\n".join(parts)
    tmp = os.path.join(os.environ.get("TEMP", os.getcwd()), "_agent_context.md")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    return tmp


async def main_bridge(port: int):
    """Persistent bridge: one ClaudeSDKClient, multi-turn, listens on TCP port."""
    from claude_agent_sdk import (
        ClaudeSDKClient,
        ClaudeAgentOptions,
        AssistantMessage,
        UserMessage,
        ResultMessage,
    )
    from claude_agent_sdk.types import TextBlock, ToolUseBlock, ToolResultBlock

    context_path = _write_context_file()
    options = ClaudeAgentOptions(
        mcp_servers=_MCP,
        system_prompt={"type": "file", "path": context_path},
        allowed_tools=_ALLOWED_TOOLS,
        permission_mode="bypassPermissions",
        max_turns=15,
    )

    query_lock = asyncio.Lock()
    _last_model = None
    _last_session_id = None

    async def send(writer, obj):
        writer.write((json.dumps(obj) + "\n").encode())
        await writer.drain()

    async with ClaudeSDKClient(options) as client:
        print(f"[agent_query] bridge connected, listening on {port}", flush=True)

        async def handle_client(reader, writer):
            nonlocal _last_model, _last_session_id
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=30.0)
                if not line:
                    return
                req = json.loads(line)
                qid = req.get("id", 0)

                if req.get("type") == "status_request":
                    mcp_status = await client.get_mcp_status()
                    ctx_usage = await client.get_context_usage()
                    servers = []
                    for s in (mcp_status or {}).get("mcpServers", []):
                        tools = s.get("tools", [])
                        si = s.get("serverInfo", {}) or {}
                        cfg = s.get("config", {}) or {}
                        servers.append(
                            {
                                "name": s.get("name", "?"),
                                "status": s.get("status", "?"),
                                "error": s.get("error") or "",
                                "scope": s.get("scope", ""),
                                "version": si.get("version", ""),
                                "config_type": cfg.get("type", ""),
                                "config_url": cfg.get("url", "")
                                or cfg.get("command", ""),
                                "tools": [
                                    {
                                        "name": t.get("name", ""),
                                        "description": t.get("description", ""),
                                        "readonly": (t.get("annotations") or {}).get(
                                            "readOnly", False
                                        ),
                                        "destructive": (t.get("annotations") or {}).get(
                                            "destructive", False
                                        ),
                                    }
                                    for t in tools
                                ],
                            }
                        )
                    cu = ctx_usage or {}
                    categories = [
                        {
                            "name": c.get("name", ""),
                            "tokens": c.get("tokens", 0),
                            "deferred": c.get("isDeferred", False),
                        }
                        for c in cu.get("categories", [])
                    ]
                    ctx = {
                        "model": cu.get("model") or _last_model or "unknown",
                        "total_tokens": cu.get("totalTokens", 0),
                        "max_tokens": cu.get("maxTokens", 0),
                        "raw_max_tokens": cu.get("rawMaxTokens", 0),
                        "percent": cu.get("percentage", 0),
                        "autocompact_enabled": cu.get("isAutoCompactEnabled", False),
                        "autocompact_threshold": cu.get("autoCompactThreshold"),
                        "autocompact_source": cu.get("autocompactSource", ""),
                        "categories": categories,
                        "memory_files": cu.get("memoryFiles", []),
                        "mcp_tools": cu.get("mcpTools", []),
                        "system_tools": cu.get("systemTools", []),
                        "system_prompt_sections": cu.get("systemPromptSections", []),
                        "api_usage": cu.get("apiUsage"),
                        "session_id": _last_session_id or "",
                    }
                    await send(
                        writer,
                        {
                            "id": qid,
                            "type": "status_data",
                            "servers": servers,
                            "context": ctx,
                        },
                    )
                    return

                prompt = req.get("prompt", "")

                async def run_query():
                    async with query_lock:
                        await client.query(prompt)
                        async for msg in client.receive_response():
                            if isinstance(msg, AssistantMessage) and msg.content:
                                if hasattr(msg, "model") and msg.model:
                                    _last_model = msg.model
                                for block in msg.content:
                                    if isinstance(block, TextBlock) and block.text:
                                        await send(
                                            writer,
                                            {
                                                "id": qid,
                                                "type": "text",
                                                "text": block.text,
                                            },
                                        )
                                    elif isinstance(block, ToolUseBlock):
                                        await send(
                                            writer,
                                            {
                                                "id": qid,
                                                "type": "tool_use",
                                                "tool_id": block.id,
                                                "name": block.name,
                                                "input": block.input,
                                            },
                                        )
                            elif isinstance(msg, UserMessage):
                                content = (
                                    msg.content if isinstance(msg.content, list) else []
                                )
                                for block in content:
                                    if isinstance(block, ToolResultBlock):
                                        await send(
                                            writer,
                                            {
                                                "id": qid,
                                                "type": "tool_result",
                                                "tool_id": block.tool_use_id,
                                                "is_error": bool(block.is_error),
                                            },
                                        )
                            elif isinstance(msg, ResultMessage):
                                if msg.session_id:
                                    _last_session_id = msg.session_id
                                cu = await client.get_context_usage() or {}
                                await send(
                                    writer,
                                    {
                                        "id": qid,
                                        "type": "done",
                                        "session_id": msg.session_id or "",
                                        "duration_ms": msg.duration_ms or 0,
                                        "cost": msg.total_cost_usd or 0.0,
                                        "num_turns": msg.num_turns or 0,
                                        "stop_reason": msg.stop_reason or "",
                                        "model": cu.get("model") or _last_model or "",
                                        "context_window": {
                                            "used_percentage": cu.get("percentage"),
                                            "total_input_tokens": cu.get("totalTokens"),
                                            "context_window_size": cu.get("maxTokens"),
                                        },
                                    },
                                )

                query_task = asyncio.create_task(run_query())

                async def watch_for_interrupt():
                    # Read any messages the client sends after the initial request.
                    # An {"type":"interrupt"} message triggers client.interrupt() so
                    # receive_response() exits naturally — no task cancellation needed.
                    try:
                        buf = b""
                        while not query_task.done():
                            data = await reader.read(1024)
                            if not data:
                                break
                            buf += data
                            while b"\n" in buf:
                                line, buf = buf.split(b"\n", 1)
                                if not line.strip():
                                    continue
                                try:
                                    msg = json.loads(line)
                                    if (
                                        msg.get("type") == "interrupt"
                                        and not query_task.done()
                                    ):
                                        await client.interrupt()
                                        return
                                except Exception:
                                    pass
                    except Exception:
                        pass

                interrupt_task = asyncio.create_task(watch_for_interrupt())
                try:
                    await query_task
                except asyncio.CancelledError:
                    try:
                        await send(writer, {"id": qid, "type": "stopped"})
                    except Exception:
                        pass
                finally:
                    interrupt_task.cancel()
                    return
            except Exception as e:
                try:
                    await send(writer, {"id": 0, "type": "error", "error": str(e)})
                except Exception:
                    pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        server = await asyncio.start_server(handle_client, "127.0.0.1", port)
        async with server:
            await server.serve_forever()


async def main(prompt: str):
    from claude_agent_sdk import (
        query,
        ClaudeAgentOptions,
        AssistantMessage,
        ResultMessage,
    )

    options = ClaudeAgentOptions(
        mcp_servers=_MCP,
        system_prompt={"type": "file", "path": _write_context_file()},
        allowed_tools=_ALLOWED_TOOLS,
        permission_mode="bypassPermissions",
        max_turns=15,
    )
    got_text = False
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage) and msg.content:
            for block in msg.content:
                if hasattr(block, "text") and block.text:
                    sys.stdout.write(block.text)
                    sys.stdout.flush()
                    got_text = True
        elif isinstance(msg, ResultMessage) and msg.result and not got_text:
            sys.stdout.write(msg.result)
            sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--bridge":
        asyncio.run(main_bridge(int(sys.argv[2])))
    else:
        prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Say hello."
        asyncio.run(main(prompt))
