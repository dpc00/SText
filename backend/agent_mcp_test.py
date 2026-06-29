"""agent_mcp_test.py — Verify Agent SDK can call sublime-mcp tools.

Usage: python agent_mcp_test.py
"""

import asyncio
from claude_agent_sdk import query, ClaudeAgentOptions


async def main():
    options = ClaudeAgentOptions(
        mcp_servers={"sublime-mcp": {"type": "sse", "url": "http://127.0.0.1:9502/sse"}},
        permission_mode="bypassPermissions",
        max_turns=3,
    )
    async for message in query(
        prompt="Use the sublime-mcp get_active_file tool and tell me just the filename (not path) of the currently active file in Sublime Text.",
        options=options,
    ):
        t = type(message).__name__
        if hasattr(message, "result") and message.result:
            print(f"RESULT: {message.result}")
        elif hasattr(message, "content"):
            for block in (message.content or []):
                if hasattr(block, "text"):
                    print(f"TEXT: {block.text}")


if __name__ == "__main__":
    asyncio.run(main())
