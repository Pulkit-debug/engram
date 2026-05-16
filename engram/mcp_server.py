"""Engram MCP server — DevOps context and blast-radius layer.

Eight tools exposed over MCP stdio:

  blast_radius     — Risk assessment BEFORE any destructive operation. The
                     killer feature: would have prevented DataTalks.Club.
  infra_context    — Structured cross-repo context for a service or file,
                     token-budget aware.
  trace_env_var    — Where is an env var defined? Where is it consumed?
  dependents_of    — Reverse-lookup. "What services use Redis?"
  infra_diff       — Infra files changed in the last N days.
  service_map      — Compact dependency graph for one or all services.
  verify_context   — Anti-poisoning check on Engram-emitted artifacts.
  remember/recall/forget — Persistent memory for DevOps decisions.

Run directly: python -m engram.mcp_server
Or via CLI:   engram serve
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from engram.config import load_config
from engram.db import open_db

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="engram",
    instructions=(
        "Engram is a local-first DevOps context and safety layer for AI coding agents. "
        "It knows about every Dockerfile, Terraform module, Kubernetes manifest, Helm "
        "chart, Jenkinsfile, docker-compose file, and env file across every repo on "
        "this machine, and it knows which resources are in production. "
        "\n\n"
        "MANDATORY: Before running any destructive operation against infrastructure "
        "(terraform destroy, kubectl delete, helm uninstall, docker rm, rm -rf on "
        "infra files), call `blast_radius` FIRST and follow its recommendation. "
        "Past production incidents (DataTalks.Club Feb 2026, PocketOS April 2026) "
        "involved agents acting on infrastructure without this check. "
        "\n\n"
        "When asked about cross-repo relationships, env vars, or service dependencies, "
        "prefer Engram tools over reading files individually — they save tokens and "
        "produce more accurate structural answers."
    ),
)


# Module-level cache. Initialized on first tool call.
_conn = None
_config = None


def _init():
    """Lazy DB + config init. One connection per process lifetime."""
    global _conn, _config
    if _conn is None:
        _config = load_config()
        _conn = open_db(_config)
    return _conn, _config


# ---------------------------------------------------------------------------
# Tool 1: blast_radius — the killer feature.
# ---------------------------------------------------------------------------

@mcp.tool()
def blast_radius(operation: str, target: str) -> str:
    """Assess the blast radius of an operation BEFORE executing it.

    Call this BEFORE any destructive infrastructure operation. Returns a
    structured risk assessment: tier (green/orange/red), recommended action
    (proceed/confirm/block), the list of dependent resources, the inferred
    environment, recent incident references, and human-readable reasons.

    Args:
        operation: The operation to assess. Examples: "terraform destroy",
                   "kubectl delete deployment payments", "helm uninstall api",
                   "rm -rf prod/", "docker rm -f db".
        target:    The thing being operated on. Examples: "payments-service",
                   "production", "prod-db", "./terraform/main.tf".

    Returns: JSON string with the full BlastRadiusResult.
    """
    from engram.safety.blast_radius import assess

    conn, _ = _init()
    result = assess(conn, operation, target)

    payload = {
        "operation": result.operation,
        "operation_class": result.operation_class,
        "target": result.target,
        "environment": result.environment,
        "risk_tier": result.risk_tier,
        "action": result.action,
        "dependents": result.dependents,
        "resolved_resources": [
            {"uid": r["uid"], "kind": r["kind"], "name": r["name"],
             "namespace": r.get("namespace", ""), "file_path": r["file_path"]}
            for r in result.resolved_resources
        ],
        "incident_refs": result.incident_refs,
        "reasons": result.reasons,
    }
    return json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# Tool 2: infra_context — structured cross-repo context.
# ---------------------------------------------------------------------------

@mcp.tool()
def infra_context(target: str, token_budget: int = 2000) -> str:
    """Return structured infrastructure context for a service or file.

    Replaces 5-10 raw file reads with one compressed payload covering:
    summary, every resource (across formats), env vars (defined and consumed
    cross-file), 1-hop dependencies, and recent changes — compressed to fit
    `token_budget` (approximate, in tokens).

    Args:
        target:       Service name, file path, or partial path.
        token_budget: Approximate token budget for the response (default 2000).
    """
    from engram.mcp_tools.context import build_infra_context

    conn, _ = _init()
    payload = build_infra_context(conn, target, token_budget=token_budget)
    return json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# Tool 3: trace_env_var
# ---------------------------------------------------------------------------

@mcp.tool()
def trace_env_var(name: str) -> str:
    """Find every place an env var is defined or consumed across all repos.

    Args:
        name: The env var name (case-sensitive convention: usually UPPERCASE).
    """
    from engram.graph import trace_env_var as _trace

    conn, _ = _init()
    data = _trace(conn, name)
    return json.dumps({
        "var_name": name,
        "defined_in": [
            {"file": r["file_path"], "value": r.get("value", ""),
             "project": r.get("project_path", "")}
            for r in data["defined_in"]
        ],
        "referenced_in": [
            {"file": r["file_path"], "project": r.get("project_path", "")}
            for r in data["referenced_in"]
        ],
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool 4: dependents_of
# ---------------------------------------------------------------------------

@mcp.tool()
def dependents_of(target: str) -> str:
    """Reverse-lookup: what depends on this technology/service/resource?

    Examples: dependents_of("postgres") -> services that use Postgres.
              dependents_of("auth-service") -> services that depend on auth.

    Args:
        target: A service name, resource name, or technology name.
    """
    from engram.graph import find_resource_by_name
    from engram.safety.blast_radius import collect_dependents

    conn, _ = _init()
    resources = find_resource_by_name(conn, target)

    # Also check technology table.
    tech_row = conn.execute(
        "SELECT name FROM technology WHERE name = lower(?)", (target,)
    ).fetchone()

    all_dependents: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for r in resources:
        for dep in collect_dependents(conn, "resource", r["uid"], hops=1, limit=100):
            key = (dep["kind"], dep["id"])
            if key not in seen:
                seen.add(key)
                all_dependents.append(dep)

    if tech_row:
        for dep in collect_dependents(conn, "technology", tech_row["name"], hops=1, limit=100):
            key = (dep["kind"], dep["id"])
            if key not in seen:
                seen.add(key)
                all_dependents.append(dep)

    return json.dumps({
        "target": target,
        "dependent_count": len(all_dependents),
        "dependents": all_dependents,
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool 5: infra_diff
# ---------------------------------------------------------------------------

@mcp.tool()
def infra_diff(days: int = 7) -> str:
    """List infrastructure files changed in the last N days.

    Args:
        days: Look-back window in days. Default 7.
    """
    from datetime import datetime, timedelta, timezone

    conn, _ = _init()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT f.path, f.name, f.modified_at, f.risk_tier, f.project_path,
               p.name AS project_name
        FROM file f
        LEFT JOIN project p ON p.path = f.project_path
        WHERE f.modified_at >= ?
        ORDER BY f.modified_at DESC
        LIMIT 200
        """,
        (cutoff,),
    ).fetchall()
    return json.dumps({
        "window_days": days,
        "count": len(rows),
        "files": [dict(r) for r in rows],
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool 6: service_map
# ---------------------------------------------------------------------------

@mcp.tool()
def service_map(scope: str = "all") -> str:
    """Compact dependency map between services across all infrastructure formats.

    Returns adjacency list + Mermaid diagram source. Each node carries the
    formats it spans, its environments, and the worst risk tier seen across
    its resources.

    Args:
        scope: 'all' for the full map, or a service name to narrow to one
               service and its direct neighbors.
    """
    from engram.mcp_tools.service_map import build_service_map

    conn, _ = _init()
    return json.dumps(build_service_map(conn, scope), indent=2)


# ---------------------------------------------------------------------------
# Tool 7: verify_context — anti-poisoning.
# ---------------------------------------------------------------------------

@mcp.tool()
def verify_context(file_path: str) -> str:
    """Verify an Engram-emitted artifact (AGENTS.md, CLAUDE.md, INFRA.md).

    Returns whether the file's current contents match the signature Engram
    recorded when it last emitted the file. Detects tampering and drift.

    Args:
        file_path: Path to the artifact to verify.
    """
    from pathlib import Path
    from engram.output.codified import detect_drift

    conn, cfg = _init()
    row = conn.execute(
        "SELECT kind, content_hash, signature, emitted_at FROM emission WHERE path = ?",
        (file_path,),
    ).fetchone()
    if not row:
        return json.dumps({
            "file_path": file_path,
            "verified": False,
            "reason": "no emission record for this path — Engram never wrote this file",
        }, indent=2)

    drift = detect_drift(conn, cfg, Path(file_path))
    return json.dumps({
        "file_path": file_path,
        "verified": not drift.get("drift", True),
        "kind": row["kind"],
        "emitted_at": row["emitted_at"],
        "on_disk_signature": drift.get("on_disk_signature"),
        "recorded_signature": row["signature"],
        "current_signature": drift.get("current_signature"),
        "drift_reason": drift.get("reason"),
    }, indent=2)


# ---------------------------------------------------------------------------
# Tools 8-10: memory.
# ---------------------------------------------------------------------------

@mcp.tool()
def remember(
    content: str,
    memory_type: str = "observation",
    service: str = "",
    project: str = "",
    file_path: str = "",
) -> str:
    """Store a DevOps observation, decision, convention, error, or runbook.

    Args:
        content:     The thing to remember (plain text, be specific).
        memory_type: One of: observation, decision, convention, error, runbook.
        service:     Optional service this memory relates to.
        project:     Optional project path.
        file_path:   Optional file path.
    """
    import hashlib
    from datetime import datetime, timezone

    conn, _ = _init()
    valid = {"observation", "decision", "convention", "error", "runbook"}
    if memory_type not in valid:
        memory_type = "observation"
    mid = hashlib.sha256(
        f"{content[:200]}|{memory_type}|{service}|{project}".encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO memory(id, content, memory_type, source, project_path,
                           file_path, service_name, created_at, accessed_at,
                           access_count, importance)
        VALUES (?, ?, ?, 'mcp', ?, ?, ?, ?, ?, 0, 1.0)
        ON CONFLICT(id) DO UPDATE SET
            content=excluded.content,
            accessed_at=excluded.accessed_at,
            access_count=memory.access_count + 1
        """,
        (mid, content, memory_type, project, file_path, service, now, now),
    )
    return json.dumps({"id": mid, "memory_type": memory_type, "stored": True}, indent=2)


@mcp.tool()
def recall(query: str, service: str = "", limit: int = 10) -> str:
    """Retrieve memories relevant to a query.

    Args:
        query:   What you want to remember.
        service: Optional service scope.
        limit:   Max memories to return.
    """
    conn, _ = _init()
    q = f"%{query.lower()}%"
    if service:
        rows = conn.execute(
            """
            SELECT id, content, memory_type, service_name, project_path, created_at
            FROM memory
            WHERE service_name = ? AND lower(content) LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (service, q, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, content, memory_type, service_name, project_path, created_at
            FROM memory
            WHERE lower(content) LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (q, limit),
        ).fetchall()
    return json.dumps({
        "query": query,
        "count": len(rows),
        "memories": [dict(r) for r in rows],
    }, indent=2)


@mcp.tool()
def forget(memory_id: str) -> str:
    """Delete a memory by id."""
    conn, _ = _init()
    cur = conn.execute("DELETE FROM memory WHERE id = ?", (memory_id,))
    return json.dumps({"id": memory_id, "deleted": cur.rowcount > 0}, indent=2)


# ---------------------------------------------------------------------------
# Lifecycle.
# ---------------------------------------------------------------------------

def run_server() -> None:
    """Start the MCP server with stdio transport."""
    _init()
    mcp.run(transport="stdio")


def main() -> None:
    run_server()


if __name__ == "__main__":
    main()
