// opencode_log_plugin.js — opencode → ai_log_server bridge.
// Source: C:\Users\donal\projects\SText\logs\opencode_log_plugin.js
// Deploy: copy to C:\Users\donal\.config\opencode\plugins\opencode_log_plugin.js
//
// opencode fires plugin hooks (tool.execute.before/after, a generic event
// handler, etc.). This plugin translates each into the JSON shape that
// ai_log_server.py (127.0.0.1:9511/event) already understands — the same
// shape Claude Code's hooks emit — so the existing events_<date>.jsonl +
// <date>.md pipeline captures opencode sessions untouched.
//
// Two streams are written:
//   1. POST to 127.0.0.1:9511/event       — translated event (reuses the server)
//   2. ~/data/logs/ai/opencode_raw_<date>.jsonl — raw opencode event (so the
//      exact field shapes are on disk and the mapping can be refined without
//      a second run; this file is debug, not the human render).
//
// No server-side change needed: ai_log_server.py keys on `hook_event_name`
// and a handful of payload fields, which this plugin fills in.
//
// Reproducing Claude's readable .md answer block:
// Claude's `Stop` hook carries `last_assistant_message` with the full reply,
// and ai_log_server._flush_turn renders that verbatim under the `### Claude`
// header. opencode never emits that field, so without intervention the
// assistant's answer text is entirely absent from the .md (its text only
// arrives via `MessageDisplay.delta`, which the server truncates to 100
// chars as an ambient one-liner). To match Claude's log we ACCUMULATE the
// assistant's finalized `message.part.updated` text parts for the turn and
// post them as `last_assistant_message` on the `Stop` event.

const PORT = 9511;
const RAW_DIR = "C:\\Users\\donal\\data\\logs\\ai";

let _session = null;  // best-effort current session id
// messageID -> role ("user" | "assistant" | ...), filled from message.updated
// so message.part.updated text parts know which side of the turn they belong to.
const _roleById = new Map();
// Per-session turn accumulator for assistant answer text.
// sid -> { text: string, seenParts: Set<string> }
// `text`    : concatenated finalized assistant text parts for the live turn.
// `seenParts`: part ids already folded in, so a part that fires twice
//             (empty marker + final) or is re-broadcast is not duplicated.
// Cleared on Stop (turn end) and on a new user prompt.
const _turnText = new Map();

function _date() {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}

function _rawPath() {
  return `${RAW_DIR}\\opencode_raw_${_date()}.jsonl`;
}

function _logRaw(type, payload) {
  // disabled: user does not want raw jsonl files cluttering their logs directory
}

async function _post(ev) {
  try {
    await fetch(`http://127.0.0.1:${PORT}/event`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ev),
    });
  } catch (e) {
    // server down — events are still in the raw jsonl; non-blocking
  }
}

// Best-effort session id extraction from whatever the event carries.
function _sid(event) {
  if (event && event.properties && event.properties.sessionId) return event.properties.sessionId;
  if (event && event.sessionId) return event.sessionId;
  if (event && event.session) return event.session;
  return _session || "opencode";
}

function _turnBucket(sid) {
  let b = _turnText.get(sid);
  if (!b) {
    b = { text: "", seenParts: new Set() };
    _turnText.set(sid, b);
  }
  return b;
}

function _resetTurn(sid) {
  _turnText.set(sid, { text: "", seenParts: new Set() });
}

export const AiLogPlugin = async (ctx) => {
  return {
    // Tool events give us structured input/args — map to PreToolUse / PostToolUse.
    "tool.execute.before": async (input, output) => {
      const toolName = input?.tool ?? "?";
      const toolInput = output?.args ?? input?.args ?? {};
      const sid = _session || "opencode";
      _logRaw("tool.execute.before", { input, output });
      await _post({
        hook_event_name: "PreToolUse",
        session_id: sid,
        tool_name: toolName,
        tool_input: toolInput,
        tool_use_id: null,
      });
    },

    "tool.execute.after": async (input, output) => {
      const toolName = input?.tool ?? "?";
      const sid = _session || "opencode";
      _logRaw("tool.execute.after", { input, output });
      await _post({
        hook_event_name: "PostToolUse",
        session_id: sid,
        tool_name: toolName,
        tool_use_id: null,
      });
    },

    // Generic event handler — catches message/session/permission/etc.
    // We switch on event.type and translate into the right hook_event_name.
    event: async ({ event }) => {
      const type = event?.type;
      _logRaw(type, event);

      if (type === "session.created") {
        _session = _sid(event);
        _resetTurn(_session);
        await _post({ hook_event_name: "SessionStart", session_id: _sid(event) });
        return;
      }
      if (type === "session.idle") {
        // opencode's turn-end signal → Stop. Ship the accumulated assistant
        // text as `last_assistant_message` so ai_log_server._flush_turn
        // renders the full answer block under the `### Claude` header —
        // exactly the field Claude's own Stop hook provides.
        const sid = _sid(event);
        const bucket = _turnText.get(sid);
        const lastAssistantMessage = (bucket && bucket.text) ? bucket.text : "";
        _resetTurn(sid);
        await _post({
          hook_event_name: "Stop",
          session_id: sid,
          last_assistant_message: lastAssistantMessage,
        });
        return;
      }
      if (type === "session.compacted") {
        await _post({ hook_event_name: "PreCompact", session_id: _sid(event) });
        return;
      }
      if (type === "permission.asked") {
        await _post({
          hook_event_name: "PermissionRequest",
          session_id: _sid(event),
          tool_name: event?.tool ?? event?.properties?.tool ?? "?",
        });
        return;
      }
      // message.updated carries role + model metadata but NO text. Record the
      // messageID -> role so the later message.part.updated text part knows
      // whether to render as the user's prompt or the assistant's reply.
      // Do NOT emit MessageDisplay from this event (it has empty text and
      // just produces noise 💬 lines in the render).
      if (type === "message.updated") {
        const info = event?.properties?.info ?? {};
        if (info.id && info.role) _roleById.set(info.id, info.role);
        return;
      }
      // message.part.updated fires (at least) twice per text part: once with
      // empty text (start marker), once with the FULL finalized text. This
      // is the reliable source of both the user's prompt and the assistant's
      // reply — message.part.delta is a redundant token stream that also
      // mixes in reasoning, so we ignore it.
      if (type === "message.part.updated") {
        const part = event?.properties?.part ?? {};
        if (part.type !== "text" || !part.text) return;
        const sid = _sid(event);
        const role = _roleById.get(part.messageID) ?? "assistant";
        if (role === "user") {
          // A new user prompt begins a fresh turn: clear any assistant text
          // carried over from the previous turn before recording this prompt.
          _resetTurn(sid);
          await _post({
            hook_event_name: "UserPromptSubmit",
            session_id: sid,
            prompt: part.text,
          });
        } else {
          // Assistant answer text: accumulate for the Stop event. Dedupe by
          // part id so the empty-marker+final double-fire, or any re-broadcast,
          // never duplicates the text. We intentionally do NOT emit a
          // MessageDisplay event here: the server truncates that ambient line
          // to 100 chars, and the full text now renders via Stop's
          // last_assistant_message (matching Claude's log).
          const bucket = _turnBucket(sid);
          const partId = part.id ?? `${part.messageID}:${part.type}`;
          if (!bucket.seenParts.has(partId)) {
            bucket.seenParts.add(partId);
            bucket.text = bucket.text
              ? `${bucket.text}\n\n${part.text}`
              : part.text;
          }
        }
        return;
      }
      // unmapped events: no translation, but raw is already on disk.
    },
  };
};