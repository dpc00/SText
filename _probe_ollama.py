"""Look inside the ollama GUI's sqlite for cached usage/quota info."""
import json
import os
import sqlite3

out = []
path = os.path.expandvars(r"%LOCALAPPDATA%\Ollama\db.sqlite")
conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
try:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    out.append("tables: %s" % tables)
    for table in tables:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
        out.append("%s: %s" % (table, cols))
        interesting = [c for c in cols if any(
            k in c.lower() for k in ("usage", "quota", "limit", "plan", "cloud"))]
        if interesting:
            rows = conn.execute(
                "SELECT %s FROM %s LIMIT 5" % (", ".join(interesting), table)
            ).fetchall()
            out.append("  sample: %r" % (rows,))
finally:
    conn.close()

out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_probe_out.txt")
with open(out_path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out))
