"""service_map: produces a compact, cross-format service dependency graph.

A "service" here is a logical name that may span multiple infrastructure
formats (the cross-format detection from the codified-context emitter).
We then walk DEPENDS_ON / USES edges between resources belonging to those
services and produce:

  * adjacency list: { service_name: [dependent_service, ...] }
  * mermaid source for visualization

Designed to fit in <2KB of JSON for a typical 10-service graph.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def build_service_map(conn: sqlite3.Connection, scope: str = "all") -> dict[str, Any]:
    """Build the service dependency graph.

    Args:
        scope: 'all' or a service name. When narrowed, returns just that
               service and its direct neighbors.
    """
    # Step 1: detect cross-format services (resource names that span >=2 kinds).
    rows = conn.execute("""
        SELECT name, count(DISTINCT kind) AS kind_count
        FROM resource
        WHERE name <> ''
        GROUP BY name
        HAVING kind_count >= 1
    """).fetchall()
    all_service_names = {r["name"] for r in rows}

    if scope != "all" and scope not in all_service_names:
        return {
            "scope": scope,
            "found": False,
            "hint": f"No service named '{scope}'. Use scope='all' to see everything.",
        }

    # Step 2: build name → resource_uids map.
    name_to_uids: dict[str, set[str]] = {}
    res_rows = conn.execute(
        "SELECT uid, name, kind, environment FROM resource WHERE name <> ''"
    ).fetchall()
    for r in res_rows:
        name_to_uids.setdefault(r["name"], set()).add(r["uid"])

    # Reverse map: uid → service name.
    uid_to_name: dict[str, str] = {}
    for name, uids in name_to_uids.items():
        for uid in uids:
            uid_to_name[uid] = name

    # Step 3: walk DEPENDS_ON / USES edges between resources.
    edge_rows = conn.execute(
        """
        SELECT src_id, dst_id FROM edge
        WHERE src_kind = 'resource' AND dst_kind = 'resource'
          AND rel_type IN ('DEPENDS_ON', 'USES')
        """
    ).fetchall()

    adjacency: dict[str, set[str]] = {}
    for e in edge_rows:
        src_name = uid_to_name.get(e["src_id"])
        dst_name = uid_to_name.get(e["dst_id"])
        if not src_name or not dst_name or src_name == dst_name:
            continue
        adjacency.setdefault(src_name, set()).add(dst_name)

    # Step 4: narrow by scope if requested.
    if scope != "all":
        neighbors = set(adjacency.get(scope, set()))
        # Plus in-edges to `scope`.
        for src, dsts in adjacency.items():
            if scope in dsts:
                neighbors.add(src)
        narrowed = {scope: adjacency.get(scope, set())}
        for n in neighbors:
            narrowed[n] = adjacency.get(n, set())
        adjacency = narrowed

    # Step 5: enrich each node with format set + worst risk_tier.
    node_meta: dict[str, dict[str, Any]] = {}
    for name in adjacency:
        meta = _service_meta(conn, name)
        node_meta[name] = meta
    # Include leaf nodes (services that only appear as dst).
    seen = set(adjacency.keys())
    leaves = {d for dsts in adjacency.values() for d in dsts if d not in seen}
    for name in leaves:
        node_meta[name] = _service_meta(conn, name)

    # Step 6: render.
    mermaid = _render_mermaid(adjacency, node_meta)

    return {
        "scope": scope,
        "found": True,
        "node_count": len(node_meta),
        "edge_count": sum(len(v) for v in adjacency.values()),
        "nodes": node_meta,
        "adjacency": {k: sorted(v) for k, v in adjacency.items()},
        "mermaid": mermaid,
    }


def _service_meta(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    """Aggregate {formats, environments, worst tier} across all resources with this name."""
    rows = conn.execute(
        "SELECT kind, environment, risk_tier FROM resource WHERE name = ?", (name,),
    ).fetchall()
    formats = sorted({r["kind"].split(":", 1)[0] for r in rows})
    envs = sorted({r["environment"] for r in rows if r["environment"]})
    tiers = {r["risk_tier"] for r in rows}
    worst = "red" if "red" in tiers else ("orange" if "orange" in tiers else "green")
    return {
        "formats": formats,
        "environments": envs,
        "risk_tier": worst,
        "resource_count": len(rows),
    }


def _render_mermaid(
    adjacency: dict[str, set[str]],
    nodes: dict[str, dict[str, Any]],
) -> str:
    """Render a Mermaid graph TD/LR diagram source string."""
    lines = ["graph LR"]
    # Style classes for tiers.
    lines.append("    classDef red    fill:#ffd5d5,stroke:#c0392b,color:#000")
    lines.append("    classDef orange fill:#ffe7b3,stroke:#d68910,color:#000")
    lines.append("    classDef green  fill:#d5f5dd,stroke:#27ae60,color:#000")

    # Sanitize node ids for Mermaid (alphanumeric + underscore).
    def sid(name: str) -> str:
        return "n_" + "".join(c if c.isalnum() else "_" for c in name)

    # Nodes.
    for name, meta in sorted(nodes.items()):
        node_id = sid(name)
        label = name
        if meta.get("environments"):
            label = f"{name}<br/><small>{', '.join(meta['environments'])}</small>"
        lines.append(f'    {node_id}["{label}"]')
        tier = meta.get("risk_tier", "green")
        lines.append(f"    class {node_id} {tier}")

    # Edges.
    for src, dsts in sorted(adjacency.items()):
        for dst in sorted(dsts):
            lines.append(f"    {sid(src)} --> {sid(dst)}")

    return "\n".join(lines)
