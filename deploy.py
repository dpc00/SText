"""Deploy this repo into Sublime's live ``Packages/User`` directory.

Why this exists
----------------
The repo and the live plugin are two independent copies. Editing one and
forgetting the other is the default failure mode, and the symptom (Sublime
running yesterday's code while the tests pass against today's) is invisible.
This script makes the copy an explicit, reviewable step.

Design notes
------------
* **Repo is the source of truth.** Only repo -> live. Never the reverse: a
  reverse sync would quietly resurrect whatever was hand-edited in the live
  copy, which is exactly how the two drifted apart in the first place.
* **--check is the default.** Nothing is written until you pass ``--apply``,
  so this is safe to run (and to wire into a pre-commit hook) at any time.
* **Newer-live files abort.** If a live file is newer than its repo twin, the
  user probably edited the live copy directly and copying over it would lose
  work. Say so and stop rather than silently overwriting; ``--force`` opts in.
* **Local-only files are never deleted.** Sublime keeps user settings, licence
  state and other packages' files in the same directory. Deleting anything not
  in the repo would be catastrophic, so this only ever adds and overwrites the
  specific paths the repo owns.
* **No subdir walk, no hand-maintained file list.** The repo is flat (one
  plugin command per top-level .py file, no subdirs) specifically so nothing
  has to remember to register a new file anywhere. A top-level .py is "owned"
  (and deployed) iff it imports sublime_plugin -- that's what distinguishes
  an actual ST plugin file from an adhoc root-level script (e.g. a one-off
  probe script) that isn't meant to go live. Adding a new command file is
  enough; this script picks it up automatically, no list to edit here.

Usage
-----
    python deploy.py            # report what would change
    python deploy.py --apply    # do it
    python deploy.py --apply --force   # overwrite newer live files too
"""

import argparse
import filecmp
import os
import shutil
import sys


REPO = os.path.dirname(os.path.abspath(__file__))

# Non-.py resources the repo owns outright (not auto-discovered, since
# nothing about their content signals "this belongs to Sublime").
OWNED_RESOURCE_FILES = (
    "Main.sublime-menu",
    "Context.sublime-menu",
    "Default.sublime-commands",
    "Default.sublime-keymap",
)

SKIP_SUFFIXES = (".pyc", ".pyo", ".orig", ".rej")


def default_target():
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, "Sublime Text", "Packages", "User")
    # macOS / Linux fallbacks, in the order Sublime itself uses.
    home = os.path.expanduser("~")
    for candidate in (
        os.path.join(home, "Library", "Application Support", "Sublime Text", "Packages", "User"),
        os.path.join(home, ".config", "sublime-text", "Packages", "User"),
    ):
        if os.path.isdir(candidate):
            return candidate
    return candidate


def _is_plugin_file(path):
    """A top-level .py is repo-owned iff it imports sublime_plugin -- that's
    what separates a real ST plugin command from an adhoc dev/probe script
    sitting at repo root (e.g. _probe_ollama.py), without needing a
    hand-maintained allowlist that the flat-file convention exists to avoid."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return False
    # An actual import statement, not just the string appearing anywhere
    # (e.g. in a comment/docstring describing this very mechanism -- this
    # script's own docstring above did exactly that and self-selected as
    # "owned" until this was tightened).
    return any(
        line.startswith("import sublime_plugin") or line.startswith("from sublime_plugin")
        for line in lines
    )


def owned_paths():
    """Every repo-relative file this script is allowed to write."""
    out = []
    for name in OWNED_RESOURCE_FILES:
        if os.path.isfile(os.path.join(REPO, name)):
            out.append(name)
    for fn in sorted(os.listdir(REPO)):
        if not fn.endswith(".py") or fn.endswith(SKIP_SUFFIXES):
            continue
        full = os.path.join(REPO, fn)
        if os.path.isfile(full) and _is_plugin_file(full):
            out.append(fn)
    return sorted(out)


def classify(rel, target):
    """(state, src, dst) for one repo-relative path.

    States: ``same``, ``new`` (absent live), ``update`` (live older),
    ``conflict`` (live newer than repo).
    """
    src = os.path.join(REPO, rel.replace("/", os.sep))
    dst = os.path.join(target, rel.replace("/", os.sep))
    if not os.path.exists(dst):
        return "new", src, dst
    if filecmp.cmp(src, dst, shallow=False):
        return "same", src, dst
    if os.path.getmtime(dst) > os.path.getmtime(src) + 1:
        return "conflict", src, dst
    return "update", src, dst


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="actually copy files")
    ap.add_argument("--force", action="store_true", help="overwrite newer live files")
    ap.add_argument("--target", default=None, help="Packages/User directory")
    args = ap.parse_args(argv)

    target = args.target or default_target()
    if not os.path.isdir(target):
        print("deploy: target does not exist: %s" % target)
        return 2

    buckets = {"same": [], "new": [], "update": [], "conflict": []}
    plan = []
    for rel in owned_paths():
        state, src, dst = classify(rel, target)
        buckets[state].append(rel)
        if state in ("new", "update") or (state == "conflict" and args.force):
            plan.append((rel, src, dst))

    print("deploy: %s" % target)
    for state in ("new", "update", "conflict"):
        for rel in buckets[state]:
            print("  %-9s %s" % (state, rel))
    print(
        "  (%d same, %d new, %d update, %d conflict)"
        % (
            len(buckets["same"]),
            len(buckets["new"]),
            len(buckets["update"]),
            len(buckets["conflict"]),
        )
    )

    if buckets["conflict"] and not args.force:
        print(
            "\ndeploy: refusing to overwrite files that are NEWER live than in the\n"
            "repo -- they were probably edited in place. Copy those changes back\n"
            "into the repo first, or re-run with --force to discard them."
        )
        return 1

    if not plan:
        print("\ndeploy: already in sync")
        return 0
    if not args.apply:
        print("\ndeploy: dry run; re-run with --apply to copy %d file(s)" % len(plan))
        return 0

    for rel, src, dst in plan:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    print("\ndeploy: copied %d file(s)" % len(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
