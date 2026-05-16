"""Dump the per-repo benchmark numbers as a markdown-friendly block.

Called by tests/benchmark_real_repos.sh with:
  argv[1] = db path
  argv[2] = alias (display name)
  argv[3] = wall-clock seconds (string)
  argv[4] = repo size in MB
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter


def main() -> None:
    db_path = sys.argv[1]
    alias = sys.argv[2]
    elapsed = sys.argv[3]
    size_mb = sys.argv[4]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    counts = {}
    for table in ("project", "file", "resource", "entity", "edge", "technology"):
        counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    print(f"  Size:        {size_mb} MB")
    print(f"  Wall-clock:  {elapsed}s")
    print()
    print(f"  Projects:    {counts['project']}")
    print(f"  Files:       {counts['file']}")
    print(f"  Resources:   {counts['resource']}")
    print(f"  Entities:    {counts['entity']}")
    print(f"  Edges:       {counts['edge']}")
    print(f"  Tech:        {counts['technology']}")

    # Resource kinds breakdown.
    print()
    print("  Resource kinds (top 10):")
    rows = conn.execute(
        "SELECT kind, count(*) AS n FROM resource GROUP BY kind ORDER BY n DESC LIMIT 10"
    ).fetchall()
    for r in rows:
        print(f"    {r['kind']:35s}  {r['n']:>4}")

    # Edge rel_type breakdown.
    rows = conn.execute(
        "SELECT rel_type, count(*) AS n FROM edge GROUP BY rel_type ORDER BY n DESC"
    ).fetchall()
    print()
    print("  Edge types:")
    for r in rows:
        print(f"    {r['rel_type']:25s}  {r['n']:>4}")

    # Risk tier breakdown (resources).
    rows = conn.execute(
        "SELECT risk_tier, count(*) AS n FROM resource GROUP BY risk_tier ORDER BY n DESC"
    ).fetchall()
    print()
    print("  Risk tiers:")
    for r in rows:
        print(f"    {r['risk_tier']:8s}  {r['n']:>4}")

    # Cross-format services (resource names that span >= 2 distinct kinds).
    rows = conn.execute("""
        SELECT name, count(DISTINCT kind) AS formats, group_concat(DISTINCT kind) AS kinds
        FROM resource
        WHERE name <> ''
        GROUP BY name HAVING formats >= 2
        ORDER BY formats DESC, name
        LIMIT 15
    """).fetchall()
    print()
    print(f"  Cross-format services ({len(rows)} found, top 15):")
    for r in rows:
        # Trim long kind lists.
        kinds = r["kinds"]
        if len(kinds) > 60:
            kinds = kinds[:57] + "..."
        print(f"    {r['name']:30s}  formats={r['formats']}  {kinds}")

    # Top 5 risk-red resources.
    rows = conn.execute("""
        SELECT kind, name, environment FROM resource
        WHERE risk_tier = 'red' ORDER BY kind, name LIMIT 5
    """).fetchall()
    if rows:
        print()
        print("  Top 5 risk-red resources:")
        for r in rows:
            print(f"    {r['kind']:30s} {r['name']:25s} env={r['environment']}")
    else:
        print()
        print("  Risk-red resources: 0 (source repo has no production-tagged resources — expected)")

    # Write a JSON sidecar for the BENCHMARKS.md aggregator.
    sidecar = {
        "alias": alias,
        "size_mb": int(size_mb),
        "elapsed_s": float(elapsed),
        "counts": counts,
        "resource_kinds": [dict(r) for r in conn.execute(
            "SELECT kind, count(*) AS n FROM resource GROUP BY kind ORDER BY n DESC"
        ).fetchall()],
        "edge_types": [dict(r) for r in conn.execute(
            "SELECT rel_type, count(*) AS n FROM edge GROUP BY rel_type ORDER BY n DESC"
        ).fetchall()],
        "risk_tiers": [dict(r) for r in conn.execute(
            "SELECT risk_tier, count(*) AS n FROM resource GROUP BY risk_tier ORDER BY n DESC"
        ).fetchall()],
        "cross_format_services": [
            {"name": r["name"], "formats": r["formats"], "kinds": r["kinds"]}
            for r in conn.execute("""
                SELECT name, count(DISTINCT kind) AS formats, group_concat(DISTINCT kind) AS kinds
                FROM resource WHERE name <> ''
                GROUP BY name HAVING formats >= 2 ORDER BY formats DESC, name
            """).fetchall()
        ],
    }
    import pathlib
    out = pathlib.Path(db_path).parent / "bench.json"
    out.write_text(json.dumps(sidecar, indent=2))


if __name__ == "__main__":
    main()
