Here are the AI/LLM‑targeted packages in `C:\Users\donal\data\st_packages\repos`, grouped by what they do. I read each README to confirm rather than relying on names alone.

## Chat / assistant — talk to LLM APIs from inside the editor
- **`sublime-llm`** — "LLM for Sublime Text": chat UI for Ollama, OpenAI, Anthropic, OpenRouter, DeepSeek, and any OpenAI‑compatible endpoint. Streams, sends selections/files as context.
- **`OpenAI-sublime-text`** — "OpenAI Sublime Text Plugin": chat + inline phantoms; works with OpenAI Responses (gpt‑5), Anthropic Claude, Google Gemini, llama.cpp, ollama, and OpenAI‑compatible hosts.
- **`pkg_Claudette`** — "Claudette": Anthropic Claude API assistant with multi‑window chat, file/selection context, model picker, system prompts, chat‑history export.
- **`Agentic`** — "LLM Agents for Sublime Text": concurrently command multiple local/remote LLMs (llama.cpp, Groq, Gemini, Claude, OpenAI) via a markdown‑file chat interface.
- **`AssistantAI`** — generic HTTP API client bundled with OpenAI (ChatGPT/Codex) presets; user‑defined servers/endpoints/prompts in settings JSON.
- **`pkg_GeminiCLI`** — agentic interface to the Google `@google/gemini-cli` tool, run from within ST.

## Inline AI code completion
- **`LSP-copilot`** — GitHub Copilot (OpenAI Codex) via `@github/copilot-language-server` on the LSP plugin — inline/panel completions + chat. Requires a Copilot subscription.
- **`pkg_Tabnine`** — official Sublime client for Tabnine.
- **`pkg_AiComplete`** — sends the current line/selection to the Google Gemini API and inserts the completion (needs `gemini_api_key`).

## LLM tooling / editor↔model plumbing
- **`pkg_HandyLLM`** — HandyLLM support: `hprompt` prompt‑file syntax highlighting, a build system to *run* hprompt files, and starter snippets (frontmatter, multimodal user msgs, tool responses). squarely prompt‑engineering tooling.
- **`Sublime-AI-Bridge`** — an MCP server hosted *inside* ST as a plugin (endpoint `http://127.0.0.1:8765/mcp`), giving an external LLM ST's symbol index, go‑to‑definition/find‑references, search, and atomic in‑buffer edits.

## Adjacent (AI‑flavored, not a chat/completion engine)
- **`sublime-deckard`** — connector to the Deckard desktop app (deckard.ai "Assist": code info/collaboration). It's a companion‑tool bridge rather than an LLM endpoint, so call it borderline.

---

## Look‑alikes that are NOT AI/LLM (flagging since the names mislead)
- **`pkg_Gemini`** — syntax highlighter for the **Gemini protocol** (text/geminispace, `.gmi`), *not* Google Gemini AI.
- **`pkg_webAgent`** — syntax highlighting for a "WebAgent" DSL, not an AI agent.
- **`pkg_MarkdownAssistant`** — markdown snippet/typography helper (`mdh1`, `mdbold`, table snippets…); no model.
- **`codic-sublime`** — wrapper for the codic.jp *naming* suggestion service, not an LLM.
- Various `*Assistant`/`*Complete*`/`*Agent*` matches (e.g. `delphin-assistant`, `SublimeAgentRansack`, `All Autocomplete`, `AutoCompleteJS`, Rails assistants) are unrelated traditional plugins — the grep noise is just shared vocabulary.

**Net: 11 clearly AI/LLM packages** (the first three groups), plus 1 adjacent (`sublime-deckard`).
