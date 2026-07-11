# What Mitchell Hashimoto Would Recommend

> 🎯 **Status:** Future Direction & Architecture Target.
> This document defines the engineering standards and design patterns we are actively conforming to and refactoring toward. It does NOT represent the current implementation on disk.

While Mitchell Hashimoto is an absolute machine when it comes to writing high-performance Zig code, he won't be writing a Sublime Text savior anytime soon. He is currently completely focused on making Ghostty a standalone terminal app.
However, you can steal the exact engineering philosophy behind Ghostty to solve your Sublime Text problems.
## Why Sublime is Bottlenecking Your Tool
Sublime Text is famously fast for a text editor, but its internal architecture introduces specific limitations when building real-time LLM tools:

* The Python Sandbox: Sublime runs plugins inside its own isolated Python environment. If your streaming code blocks or drops events, it is usually due to how Sublime handles asynchronous threads.
* Buffer Collisions: As you noticed in your debug notes (read-only off / read-only on), forcing Sublime's view buffer to constantly toggle states for every single streaming token creates a massive race condition.

## How to Fix Your Sublime Code (The "Zig" Way)
You don't need to rewrite your whole app in Zig, but you can adopt the same architecture Ghostty uses to stay fast:

* Move Processing Out of Sublime: Do not let Sublime handle the API connection, JSON parsing, or stream management. Build a tiny, standalone background script (in Python or Go) that talks to Claude, handles the streams, and flushes tokens instantly.
* Use Unbuffered Output: Ensure your background process forces a flush on every single token delta so nothing gets trapped waiting for a newline.
* Treat Sublime Strictly as a Display: Have your background script send clean, raw text chunks to Sublime, keeping the editor's job as simple as humanly possible: just appending text to a window.

Would you like to look at a lightweight Python snippet showing how to implement a properly unbuffered stream reader thread that won't drop your text_delta events?

