"""Deploy this repo into Sublime's live ``Packages/User`` directory.

Why this exists
---------------
The repo and the live plugin are two independent copies. Editing one and
forgetting the other is the default failure mode, and the symptom (Sublime
running yesterday's code while the tests pass against today's) is invisible.
This script makes the copy an explicit, reviewable step.

Design notes
------------
* **Repo is the source of truth.** Only repo → live. Never the reverse: a
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

Usage
-----
    python tools/deploy.py            # report what would change
    python tools/deploy.py --apply    # do it
    python tools/deploy.py --apply --force   # overwrite newer live files too
"""

import argparse
import filecmp
import os
import shutil
import sys


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Only these trees/files are owned by the repo. Everything else in Packages/User
# belongs to Sublime or to the user and is left strictly alone.
OWNED_DIRS = ("ai", "config", "launchers")
OWNED_FILES = (
    "Main.sublime-menu",
    "Context.sublime-menu",
    "Default.sublime-commands",
    "Default.sublime-keymap",
    "AiLog.sublime-syntax",
)

# Build artefacts and caches: present in the repo, meaningless (or harmful) live.
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", "node_modules"}
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


def owned_paths():
    """Every repo-relative file this script is allowed to write."""
    out = []
    for name in OWNED_FILES:
        if os.path.isfile(os.path.join(REPO, name)):
            out.append(name)
    for top in OWNED_DIRS:
        root_dir = os.path.join(REPO, top)
        if not os.path.isdir(root_dir):
            continue
        for root, dirs, files in os.walk(root_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fn in files:
                if fn.endswith(SKIP_SUFFIXES):
                    continue
                full = os.path.join(root, fn)
                out.append(os.path.relpath(full, REPO).replace(os.sep, "/"))
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
            "repo — they were probably edited in place. Copy those changes back\n"
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
