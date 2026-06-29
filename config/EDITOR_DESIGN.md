# ST Config Editor — Design

Status: **v1 Settings tab live (2026-06-29)** — see §9 Q3. Keybindings/Menus/
Palette tabs remain to build (§9 Q5).

## 1. Why this exists

ST's built-in Preferences editor opens two panes of raw JSON (defaults left, user
right). Reading the defaults JSON is horrible. The first attempt
(`config/st_config.py`) put CRUD on top of three file-type tabs (Commands /
Keybindings / Menus) — nice, but junky: modal-per-edit, no defaults/description
pane, an empty Menus tab, no Settings tab, and a write path that silently strips
JSON comments. The verdict: "not a polished product." This doc redesigns it from
the data model up, before any choice of implementation language.

## 2. The finding that reshapes the design

ST configuration is **not a set of file types**. It is a **graph of typed entities
joined by references**, sitting on top of a **layered override system**.
Organizing the editor by file type (the current tabs) copies ST's file layout but
not how a programmer actually thinks about a configuration. The entity graph is
the right abstraction.

Evidence from a full sweep of ST's docs + this machine's real inventory:

- **Command is the hub.** A command is a runtime object (370 registered on this
  machine, defined in `.py` plugin classes), invoked from four different file
  types: keybindings, menu items, palette entries, macros/builds. It is *not* a
  file entry — 6 of the 40 User commands have no palette entry, so a disk-only
  check throws false "dead reference" hits. **Only the runtime registered set is
  authoritative.**
- **A key-binding context references settings and scopes.** The `context` array
  is a list of `{key, operator, operand, match_all?}` triples. `key` is either
  `setting.<name>` (reads a *setting*) or a bare ST-internal state key (`selector`,
  `preceding_text`, `overlay_visible`, `num_selections`, `has_snippet`, …).
  Operators: `equal`, `not_equal`, `regex_match`, `regex_contains`,
  `not_regex_match`, `not_regex_contains`. So every keybinding silently depends on
  settings and syntax scopes.
- **A setting has a type, a default, and an effective value.** Types observed:
  `bool`, `int`, `float`, `string`, `enum-string`, `array`, `array-of-objects`,
  `scope-selector`, `path`, `encoding-name`. The effective value is a merge across
  layers: Default → Platform (`Preferences (Windows)`) → Distraction Free →
  syntax-specific → project → User.
- **Scopes are the cross-cutting currency.** Referenced by color schemes, snippets,
  completions, and key-binding contexts (`selector`).
- **ST's JSON is a dialect, not strict JSON.** It allows `//` and `/* */`
  comments, trailing commas, and control characters in string values. Every
  shipped Default file fails `json.loads`. **`sublime.decode_value` is the only
  correct parser.** Round-tripping through `json.dumps` (what the current editor
  does for commands/menus/keymaps) silently strips comments and trailing commas
  — a fidelity bug.
- **Resource resolution is layered too.** `.sublime-package` zips vs loose
  `Packages/` folders; User overrides shadow package defaults; `find_resources`
  returns virtual `Packages/<pkg>/<file>` paths across both.

## 3. The abstraction: two layers over an entity graph

The editor is split into two layers that share one fact base (the entity graph).

### Layer 1 — Editing (typed CRUD over entities)

VB6-Properties-window model, applied to every entity:

- A **name|value grid**, each value cell a typed inline editor: checkbox for
  bool, dropdown for enum, number/text for scalars, ellipsis (…) for complex
  values (arrays, objects, contexts).
- A **defaults/description pane** below the grid — the direct fix for "reading
  the JSON is horrible." For a setting: the default value + which layer it comes
  from + a description. For a command: source `.py` + args. For a keybinding: the
  conflict analysis.
- **In-context pickers for every reference field** — the core "every command
  must be look-up-able during editing" requirement: a command picker, a setting
  picker, a scope picker, a key-chord recorder, and a context-DSL builder
  (operator dropdown + operand field + match_all toggle, with the
  `setting.`/`selector`/bare-key choice surfaced).

### Layer 2 — Reasoning (graph queries over the entity graph)

A logic engine holding the entity graph as **facts** and the integrity
constraints as **rules**, answering the questions a programmer actually wades
through:

| Query | Rule (informal) |
|---|---|
| Key conflicts | two KeyBindings share a chord **and** their contexts can both be true |
| Dead reference | a KeyBinding/MenuItem/PaletteEntry/Macro invokes a Command not in the runtime set |
| Orphan settings | a `.sublime-settings` whose owning Package is not installed |
| Override chain | for a Setting, the ordered layers that define it + which one wins |
| User-vs-defaults diff | Settings where User value ≠ Default value |
| Unbound command | a registered Command with no KeyBinding, MenuItem, or PaletteEntry |
| Setting reachability | which contexts / command code read a given Setting |
| Scope usage | which ColorScheme/Snippet/Completion/Context reference a Scope |

This is the **Prolog-shaped layer.** Facts = entities + references; rules = the
constraints above. Prolog (or a small Datalog / Prolog-in-Python) is the natural
tool — declarative queries over a graph. No existing ST tool does this; it is
the novel, valuable core of the redesign.

## 4. Entity catalog

First-class entities and their fields. (Types in `monospace`.)

- **Command** — `id` (str), `scope` (enum: application/window/text),
  `source_package` (str), `source_file:line` (if discoverable), `args_schema`
  (informal — best-effort from `run()` signature or observed usage),
  `palette_captions` (list), `is_enabled_hook`, `is_checked_hook`. Edges:
  `binds`/`invokes` from KeyBinding/MenuItem/PaletteEntry/Macro/Build.
- **Setting** — `name`, `type`
  (bool/int/float/str/enum/array/object/scope-selector/path/encoding),
  `default_value` + `default_source_layer`, `effective_value` + `layer_chain`
  (ordered), `owner` (core | package), `overridden_by_user` (bool). Edges:
  `reads` from contexts + command code; `interacts_with` other settings.
- **KeyBinding** — `keys` (chord str), `command` (ref), `args` (object),
  `context` (list of constraint triples), `source_package`, `is_user` (bool).
  Edges: `binds` → Command; `reads` → Settings/Scopes via context;
  `conflicts_with` → KeyBinding.
- **MenuItem** — `caption`, `command` (ref), `args`, `checkbox`/`mnemonic`,
  `path` (breadcrumb), `menu_file`, `source_package`, `is_user`, `children`.
  Edges: `invokes` → Command.
- **PaletteEntry** — `caption`, `command` (ref), `args`, `source`. Edges:
  `invokes` → Command.
- **Scope** — `selector` (str), `defined_in` (Syntax). Edges: `used_by`
  ColorScheme/Snippet/Completion/Context.
- **Package** — `name`, `installed` (bool), `source` (zip | loose), `resources`
  (its `.sublime-*` files), `settings_file`. Edges: `defines` Commands; owns
  Settings/Keymaps/Menus.
- **Macro** — `name`, `resource_path`, `command_sequence`. Edges: `invokes` →
  Commands.
- **BuildSystem** — `name`, `selector`, `cmd`, `variants`, `target` (ref).
  Edges: `invokes` → Command (target).
- **Snippet** — `trigger`, `scope` (ref), `content`, `description`.
- **Completion** — `trigger`, `scope` (ref), `contents`.
- **Syntax** / **ColorScheme** / **Theme** — heavy; initially
  browsable/read-only.
- **Project** — `folders`, `settings_overrides`, `build_systems`.

## 5. Relationship set (the edges)

`binds` (KeyBinding→Command), `invokes`
(MenuItem/PaletteEntry/Macro/Build→Command), `reads` (Context→Setting,
Command-code→Setting), `defines` (Package→Command, Syntax→Scope), `uses_scope`
(ColorScheme/Snippet/Completion/Context→Scope), `overrides` (layer N → layer N-1;
User → Default), `conflicts_with` (KeyBinding↔KeyBinding), `dead` (any
invoke/binds → non-existent Command), `orphan` (Settings-file → uninstalled
Package).

## 6. Fact-extraction plan (runtime vs disk)

Authoritative source per entity type:

| Entity | Source | API |
|---|---|---|
| Command (registry) | **runtime** | `mcp__sublime-mcp__get_commands` / `sublime_plugin` introspection |
| Command (palette captions) | disk | `find_resources("*.sublime-commands")` + `decode_value` |
| Setting (default) | disk | `Preferences.sublime-settings` in Default.sublime-package + platform variant |
| Setting (user override) | disk | `User/*.sublime-settings` |
| Setting (effective, per-view) | **runtime** | `view.settings().get(key)` |
| KeyBinding | disk (merged) | `find_resources("*.sublime-keymap")` + platform variant + `decode_value` |
| MenuItem | disk (merged) | `find_resources("*.sublime-menu")` + `decode_value` |
| Scope (defined) | disk | `find_resources("*.sublime-syntax")` |
| Scope (used) | disk | color schemes, snippets, completions, keymap `selector` contexts |
| Package | disk + runtime | `Installed Packages/`, `Packages/`, `Package Control.sublime-settings` `installed_packages` |

**Parsing:** always `sublime.decode_value`, never `json.loads`. **Writing:**
preserve comments/trailing commas — round-trip through a comment-aware serializer
(ST's own `sublime.encode_value` does not preserve comments either; a real
solution needs a JSONC AST that patches only changed keys). **Sync:** ST
hot-reloads on file save; the editor re-extracts facts after a write (and can
listen for `on_post_save`).

## 7. UI model

- **Central browser** with a query bar: "dead references", "key conflicts",
  "settings I've overridden", "commands with no binding", "everything
  referencing command X". Each query is one Layer-2 rule.
- **Entity detail pages** showing full graph context: a Command page =
  definition + every binding/menu/palette/macro that invokes it + args +
  settings reads. A Setting page = type, default, effective value, layer chain,
  every context/code that reads it.
- **VB-style grid editor** per entity, with pickers + defaults pane.
- **Reasoning panel**: one-click inspections + a free-query mode (the Prolog
  query box).

## 8. Language placement (decided 2026-06-29)

Q4's decision (§9) settles this: v1 runs as an **in-ST Python HTTP server
serving a browser UI** — the same shape as the current `st_config.py` and as the
ccstatusline-editor the user approved. So:

- **Host / fact extraction / UI** — Python (in-ST) + HTML/CSS/JS (browser). No
  external process, no separate UI language for v1. The browser-UI medium is
  confirmed acceptable (ccstatusline-editor was liked); the junkiness was UX
  execution, not the medium.
- **Reasoning layer** — v1's five must-have queries (§9 Q2) are simple enough for
  plain Python rules. **Prolog / Datalog remains the right tool if the query set
  grows** (the user's "I might desire to program it in Prolog" maps here); it
  slots in as an upgrade when the reasoning layer gets richer, without forcing a
  Prolog dependency on day one.

Net: no language debate blocks v1. Python-in-ST + browser UI for the editor and
the first queries; Prolog as a deliberate, optional reasoning-layer upgrade.

## 9. Decisions (resolved via research 2026-06-29, so the user doesn't have to)

**Q1 — Entity list (v1).** Editable: `Setting`, `KeyBinding`, `MenuItem`,
`PaletteEntry` (with `Command` as the referenced target — browsable and
pickable, its registry from the runtime set). Browsable/read-only: `Package`,
`Project`, `Syntax`, `ColorScheme`, `Theme`; `Scope` is a picker data-source,
not a top-level editor. `Macro`/`Build`/`Snippet`/`Completion` deferred to v2.

**Q2 — Query catalog.** v1 must-have: dead command references, key conflicts
(chord + overlapping context), orphan settings, user-vs-defaults diff, override
chain. v2 nice-to-have: unbound commands, setting reachability, scope usage.

**Q3 — Comment-preserving write.** v1 target: comment-preserving round-trip via
a CST/JSONC library, fixing the current editor's fidelity bug (`json.dumps`
strips comments + trailing commas). Candidate libraries, found via research:
[json-five](https://github.com/spyoungtech/json-five) (mature, `ModelLoader`
preserves comments — primary), [jsonc-sdict](https://github.com/AClon314/jsonc-sdict)
(tree-sitter, JSONC-specific — fallback), [json5kit](https://github.com/tusharsadhwani/json5kit)
(CST-based). `commentjson` and `pyjson5` are rejected — both strip comments on
dump. **Verification gate before committing:** round-trip a real ST file (with
`//` + `/* */` comments, trailing commas, and literal control chars in string
values) and confirm (a) comments/trailing commas preserved, (b) output re-parses
cleanly with `sublime.decode_value`, (c) control chars survive. If both
libraries fail ST's dialect, fall back to in-place text-region patching (parse
for token positions, patch only changed keys' text). Worst case: match ST's
own lossy `encode_value` and document the limitation.

**Verified 2026-06-29 — json-five chosen.** Round-tripped ST's shipped
`Default (Windows).sublime-keymap` (954 lines, 27 `//` comments, 545 trailing
commas, 1460 raw tab control chars) through `json5.loader.ModelLoader` +
`json5.dumper.ModelDumper`: byte-identical output (`diff` clean once written
with `newline=''` to avoid Windows CRLF translation), comments/trailing
commas/tabs all preserved, and `sublime.decode_value` accepts the output,
parsing 356 bindings equal to the source. All three verification gates (a/b/c)
pass. jsonc-sdict/json5kit not needed.

**Refined 2026-06-29 (real-file test):** ST's *shipped* Default files are LF,
but the user's own `User/Preferences.sublime-settings` is CRLF (Windows). Writing
with `newline=''` alone normalizes CRLF→LF (because text-mode *read* translates
CRLF→`\n`, then `newline=''` write emits `\n` as LF) — a giant git diff and a
fidelity bug. Fix verified: **read with `newline=''` too** (preserve the file's
existing endings in memory), and `_add` inserts the file's detected line ending
(`_line_ending(text)` → `\r\n` or `\n`). Then set/delete/add round-trips are
byte-faithful on the real CRLF user file (md5 identical before/after a
set+delete cycle; comments + the `/*"color_scheme":...*/` block comment + CRLF
all preserved). So: **preserve existing line endings, do not force LF.**

**v1 Settings tab — LIVE 2026-06-29.** `config/settings_editor.py` (in-ST HTTP
server on port 57323 + browser UI): VB-style name|value grid with typed inline
editors (bool checkbox, enum dropdown, int/float number, string text, array/
object `✎` modal), overridden rows highlighted + revert-to-default, defaults/
source pane, search, add-setting. 196 settings extracted (Default + platform
defaults via `find_resources`/`decode_value`; user overrides via vendored
json5). Writes via the position-based set/delete/add path above. Vendored
json5 + sly into `config/lib/` (patched json5: `import regex as re` → `import re`
and dropped `\p{Pc}\p{Mn}\p{Mc}` from the NAME pattern — `regex`-lib-only Unicode
properties that ST config keys never use; `regex` ships only a cp312 `.pyd` so
can't run in ST's 3.8). Wired into `loader.py` + `Default.sublime-commands`
("Sublime Settings Editor" → `settings_editor_open`).

**Q4 — Where the fact base lives.** In-ST Python HTTP server + browser UI (the
current `st_config.py` shape; ccstatusline-editor confirms browser UI is the
accepted medium). The sublime API (runtime command set, view settings,
`decode_value`, `find_resources`) is only available in-ST, so fact extraction
stays in-ST. No external bridge for v1. A future native-UI variant could split
fact-provider from UI, mirroring the sublime-mcp bridge — not needed now.

**Q5 — v1 scope.** Settings + KeyBindings + Menus + Command Palette (the four
the user cared about, plus the missing Settings tab). Snippets/Builds/Macros/
Completions deferred.