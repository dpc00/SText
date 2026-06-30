"""mcpclient — slim async MCP client for the SText Ollama backend.

Lifted from jonigl/mcp-client-for-ollama (MIT), stripped of its rich/TUI layer.
Only the transport + tool-dispatch logic is kept; the Sublime Text view
(ai_sdk.py) is the UI. See LICENSE-ollmcp.txt for attribution.
"""