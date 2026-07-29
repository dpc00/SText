"""Rewrite bare npx/bun MCP commands to absolute Windows paths.

JVM hosts (Junie) spawn via ProcessBuilder without shell/PATHEXT resolution,
so command=\"npx\" fails with \"The system cannot find the file specified\".
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

NPX = r"C:\Program Files\nodejs\npx.cmd"
NODE = r"C:\Program Files\nodejs\node.exe"
BUN = shutil.which("bun") or str(Path.home() / ".bun" / "bin" / "bun.exe")
NODEJS_DIR = r"C:\Program Files\nodejs"


def fix_servers(servers: dict) -> int:
    changed = 0
    if not isinstance(servers, dict):
        return 0
    for _name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        cmd = cfg.get("command")
        if not cmd or not isinstance(cmd, str):
            continue
        new_cmd = cmd
        base = os.path.basename(cmd).lower()
        if base in ("npx", "npx.cmd"):
            new_cmd = NPX
        elif base in ("bun", "bun.exe"):
            new_cmd = BUN
        elif base in ("node", "node.exe") and not os.path.isabs(cmd):
            new_cmd = NODE
        if new_cmd != cmd:
            cfg["command"] = new_cmd
            changed += 1
        env = cfg.get("env")
        if isinstance(env, dict):
            path_key = "PATH" if "PATH" in env else ("Path" if "Path" in env else None)
            if path_key:
                path = env[path_key]
                if path and NODEJS_DIR.lower() not in path.lower():
                    env[path_key] = NODEJS_DIR + os.pathsep + path
                    changed += 1
    return changed


def fix_file(path: Path) -> int:
    if not path.exists():
        print(f"missing {path}")
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    total = 0
    if isinstance(data.get("mcpServers"), dict):
        total += fix_servers(data["mcpServers"])
    projects = data.get("projects")
    if isinstance(projects, dict):
        for proj in projects.values():
            if isinstance(proj, dict) and isinstance(proj.get("mcpServers"), dict):
                total += fix_servers(proj["mcpServers"])
    bak = path.with_name(path.name + ".bak-npxfix")
    if not bak.exists():
        bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{path}: changed={total}")
    return total


def main() -> None:
    home = Path.home()
    files = [
        home / ".junie" / "mcp" / "mcp.json",
        home / ".cursor" / "mcp.json",
        home / ".claude.json",
    ]
    for f in files:
        fix_file(f)
    # print commands only (no env secrets)
    for f in files:
        if not f.exists():
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        print("---", f)
        for k, v in (data.get("mcpServers") or {}).items():
            if isinstance(v, dict) and "command" in v:
                print(f"  {k} => {v['command']}")


if __name__ == "__main__":
    main()
