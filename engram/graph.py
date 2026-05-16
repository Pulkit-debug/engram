"""Graph operations: typed upserts and queries against the SQLite store.

This is the layer that all higher-level modules (crawler, MCP server,
blast_radius) call. Nothing else writes SQL directly.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Risk tier — a first-class concept, not a derived view.
# ---------------------------------------------------------------------------

RiskTier = str  # 'green' | 'orange' | 'red'

RISK_GREEN = "green"
RISK_ORANGE = "orange"
RISK_RED = "red"


# ---------------------------------------------------------------------------
# Dataclasses — the shape every module agrees on.
# ---------------------------------------------------------------------------

@dataclass
class ProjectRow:
    path: str
    name: str
    project_type: str = ""
    description: str = ""


@dataclass
class FileRow:
    path: str
    project_path: str
    name: str
    extension: str = ""
    size_bytes: int = 0
    content_hash: str = ""
    modified_at: str = ""
    risk_tier: RiskTier = RISK_GREEN


@dataclass
class ResourceRow:
    uid: str
    file_path: str
    kind: str               # e.g. "k8s:Deployment", "tf:aws_rds_instance"
    name: str
    namespace: str = ""
    environment: str = ""
    risk_tier: RiskTier = RISK_GREEN
    properties: dict[str, Any] = field(default_factory=dict)
    context_snippet: str = ""


@dataclass
class EntityRow:
    uid: str
    file_path: str
    name: str
    entity_type: str
    value: str = ""
    context_snippet: str = ""


@dataclass
class EdgeSpec:
    src_kind: str            # 'file' | 'resource' | 'entity' | 'service' | 'project'
    src_id: str
    dst_kind: str            # 'file' | 'resource' | 'entity' | 'service' | 'project' | 'technology'
    dst_id: str
    rel_type: str
    properties: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# UID helpers — stable across rebuilds.
# ---------------------------------------------------------------------------

def resource_uid(kind: str, name: str, namespace: str, file_path: str) -> str:
    raw = f"{kind}|{name}|{namespace}|{file_path}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:20]


def entity_uid(name: str, entity_type: str, file_path: str) -> str:
    raw = f"{name}|{entity_type}|{file_path}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:20]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Upserts.
# ---------------------------------------------------------------------------

def upsert_project(conn: sqlite3.Connection, row: ProjectRow) -> None:
    conn.execute(
        """
        INSERT INTO project(path, name, project_type, description, last_indexed)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            name=excluded.name,
            project_type=excluded.project_type,
            description=excluded.description,
            last_indexed=excluded.last_indexed
        """,
        (row.path, row.name, row.project_type, row.description, _now()),
    )


def upsert_file(conn: sqlite3.Connection, row: FileRow) -> None:
    # Coerce empty project_path to NULL so the FK doesn't enforce against ""
    # (a real file may not belong to any detected project).
    project_path = row.project_path or None
    conn.execute(
        """
        INSERT INTO file(path, project_path, name, extension, size_bytes,
                         content_hash, modified_at, risk_tier)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            project_path=excluded.project_path,
            name=excluded.name,
            extension=excluded.extension,
            size_bytes=excluded.size_bytes,
            content_hash=excluded.content_hash,
            modified_at=excluded.modified_at,
            risk_tier=excluded.risk_tier
        """,
        (
            row.path, project_path, row.name, row.extension,
            row.size_bytes, row.content_hash, row.modified_at, row.risk_tier,
        ),
    )


def upsert_resource(conn: sqlite3.Connection, row: ResourceRow) -> None:
    conn.execute(
        """
        INSERT INTO resource(uid, file_path, kind, name, namespace, environment,
                             risk_tier, properties, context_snippet)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uid) DO UPDATE SET
            file_path=excluded.file_path,
            kind=excluded.kind,
            name=excluded.name,
            namespace=excluded.namespace,
            environment=excluded.environment,
            risk_tier=excluded.risk_tier,
            properties=excluded.properties,
            context_snippet=excluded.context_snippet
        """,
        (
            row.uid, row.file_path, row.kind, row.name, row.namespace,
            row.environment, row.risk_tier, json.dumps(row.properties),
            row.context_snippet,
        ),
    )


def upsert_entity(conn: sqlite3.Connection, row: EntityRow) -> None:
    conn.execute(
        """
        INSERT INTO entity(uid, file_path, name, entity_type, value, context_snippet)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(uid) DO UPDATE SET
            file_path=excluded.file_path,
            name=excluded.name,
            entity_type=excluded.entity_type,
            value=excluded.value,
            context_snippet=excluded.context_snippet
        """,
        (row.uid, row.file_path, row.name, row.entity_type, row.value, row.context_snippet),
    )


def upsert_edge(conn: sqlite3.Connection, edge: EdgeSpec) -> None:
    conn.execute(
        """
        INSERT INTO edge(src_kind, src_id, dst_kind, dst_id, rel_type, properties)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(src_kind, src_id, dst_kind, dst_id, rel_type)
        DO UPDATE SET properties=excluded.properties
        """,
        (edge.src_kind, edge.src_id, edge.dst_kind, edge.dst_id,
         edge.rel_type, json.dumps(edge.properties)),
    )


def upsert_technology(conn: sqlite3.Connection, name: str, category: str = "unknown") -> None:
    conn.execute(
        """
        INSERT INTO technology(name, category) VALUES (?, ?)
        ON CONFLICT(name) DO UPDATE SET category=excluded.category
        """,
        (name.lower().strip(), category),
    )


def delete_file_and_dependents(conn: sqlite3.Connection, file_path: str) -> None:
    """Cascade-delete a file row. FK ON DELETE CASCADE handles the rest."""
    conn.execute("DELETE FROM file WHERE path = ?", (file_path,))


# ---------------------------------------------------------------------------
# Read queries — the surface MCP tools will call.
# ---------------------------------------------------------------------------

def get_file(conn: sqlite3.Connection, path: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM file WHERE path = ?", (path,)).fetchone()
    return dict(row) if row else None


def find_resource_by_name(
    conn: sqlite3.Connection, name: str, *, kind_prefix: str | None = None,
) -> list[dict[str, Any]]:
    if kind_prefix:
        rows = conn.execute(
            "SELECT * FROM resource WHERE name = ? AND kind LIKE ? ORDER BY kind",
            (name, kind_prefix + "%"),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM resource WHERE name = ? ORDER BY kind", (name,),
        ).fetchall()
    return [_inflate(r) for r in rows]


def find_resources_for_service(
    conn: sqlite3.Connection, service_name: str,
) -> list[dict[str, Any]]:
    """A service is a logical concept that may span repos. We resolve it via:
      1. Explicit resource_service mapping (if user declared it).
      2. Name-prefix heuristic (resources named `<service>-*` or matching exactly).
    """
    explicit = conn.execute(
        """
        SELECT r.* FROM resource r
        JOIN resource_service rs ON rs.resource_uid = r.uid
        WHERE rs.service_name = ?
        ORDER BY r.kind, r.name
        """,
        (service_name,),
    ).fetchall()
    if explicit:
        return [_inflate(r) for r in explicit]

    rows = conn.execute(
        """
        SELECT * FROM resource
        WHERE name = ? OR name LIKE ? OR namespace = ?
        ORDER BY kind, name
        """,
        (service_name, f"{service_name}-%", service_name),
    ).fetchall()
    return [_inflate(r) for r in rows]


def get_edges_from(
    conn: sqlite3.Connection, src_kind: str, src_id: str, rel_type: str | None = None,
) -> list[dict[str, Any]]:
    if rel_type:
        rows = conn.execute(
            "SELECT * FROM edge WHERE src_kind=? AND src_id=? AND rel_type=?",
            (src_kind, src_id, rel_type),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM edge WHERE src_kind=? AND src_id=?",
            (src_kind, src_id),
        ).fetchall()
    return [_edge_inflate(r) for r in rows]


def get_edges_to(
    conn: sqlite3.Connection, dst_kind: str, dst_id: str, rel_type: str | None = None,
) -> list[dict[str, Any]]:
    if rel_type:
        rows = conn.execute(
            "SELECT * FROM edge WHERE dst_kind=? AND dst_id=? AND rel_type=?",
            (dst_kind, dst_id, rel_type),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM edge WHERE dst_kind=? AND dst_id=?",
            (dst_kind, dst_id),
        ).fetchall()
    return [_edge_inflate(r) for r in rows]


def trace_env_var(conn: sqlite3.Connection, var_name: str) -> dict[str, list[dict[str, Any]]]:
    """Return where an env var is defined and consumed across the entire graph."""
    defs = conn.execute(
        """
        SELECT e.*, f.project_path FROM entity e
        JOIN file f ON f.path = e.file_path
        WHERE e.entity_type IN ('env_var', 'env_def') AND e.name = ?
        ORDER BY e.file_path
        """,
        (var_name,),
    ).fetchall()
    refs = conn.execute(
        """
        SELECT e.*, f.project_path FROM entity e
        JOIN file f ON f.path = e.file_path
        WHERE e.entity_type IN ('env_ref', 'env_use') AND e.name = ?
        ORDER BY e.file_path
        """,
        (var_name,),
    ).fetchall()
    return {
        "defined_in": [dict(r) for r in defs],
        "referenced_in": [dict(r) for r in refs],
    }


def graph_stats(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("project", "file", "resource", "entity", "edge",
                  "service", "technology", "memory"):
        row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
        counts[table] = int(row["n"]) if row else 0
    return counts


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _inflate(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if "properties" in d and isinstance(d["properties"], str):
        try:
            d["properties"] = json.loads(d["properties"])
        except json.JSONDecodeError:
            d["properties"] = {}
    return d


def _edge_inflate(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if "properties" in d and isinstance(d["properties"], str):
        try:
            d["properties"] = json.loads(d["properties"])
        except json.JSONDecodeError:
            d["properties"] = {}
    return d
