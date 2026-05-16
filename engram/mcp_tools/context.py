"""infra_context: compressed cross-repo context for one service or file.

Replaces 5-10 raw file reads with one compact JSON payload. Five sections:

  summary         — one-line overview (resource count, env, last modified)
  resources       — every resource matching the target (across formats)
  env_vars        — cross-file env vars referenced/defined by this service
  dependencies    — 1-hop neighbors via DEPENDS_ON / USES / MOUNTS
  recent_changes  — files touched in the last 7 days

Token-budget compression strategy (in order, until size fits):

  1. Drop `properties` from each resource (largest contributor).
  2. Drop env_var values (keep names only).
  3. Drop non-production-tier resources.
  4. Truncate dependents and env_vars lists.
  5. Truncate recent_changes.

The budget is in *characters* (not tokens) because that's deterministic. We
assume ~4 chars per token; the caller can tune.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any


def build_infra_context(
    conn: sqlite3.Connection,
    target: str,
    token_budget: int = 2000,
) -> dict[str, Any]:
    """Build the structured payload. Returns a dict ready to json.dumps."""
    from engram.graph import find_resource_by_name, find_resources_for_service

    resources = find_resource_by_name(conn, target)
    if not resources:
        resources = find_resources_for_service(conn, target)

    if not resources:
        return {
            "target": target,
            "found": False,
            "hint": "No matching resource or service found. Try `engram index` first, "
                    "or check the spelling of the service name.",
        }

    # Section: resources (compressed view).
    resources_section = [
        {
            "kind": r["kind"],
            "name": r["name"],
            "namespace": r.get("namespace", ""),
            "environment": r.get("environment", ""),
            "risk_tier": r.get("risk_tier", "green"),
            "file_path": r["file_path"],
            "properties": r.get("properties", {}),
        }
        for r in resources
    ]

    # Section: env_vars (defs + refs touching any of these files).
    file_paths = sorted({r["file_path"] for r in resources})
    env_section = _env_vars_for_files(conn, file_paths)

    # Section: dependencies (1-hop from these resources).
    deps_section = _dependencies_for_resources(conn, [r["uid"] for r in resources])

    # Section: recent_changes.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rc_rows = conn.execute(
        """
        SELECT path, modified_at FROM file
        WHERE path IN ({})
          AND modified_at >= ?
        ORDER BY modified_at DESC
        """.format(",".join("?" * len(file_paths))),
        (*file_paths, cutoff),
    ).fetchall() if file_paths else []
    recent_changes = [{"path": r["path"], "modified_at": r["modified_at"]} for r in rc_rows]

    # Section: summary.
    envs = {r.get("environment", "") for r in resources if r.get("environment")}
    tiers = {r.get("risk_tier", "green") for r in resources}
    summary = {
        "target": target,
        "resource_count": len(resources),
        "environments": sorted(e for e in envs if e),
        "risk_tier": "red" if "red" in tiers else ("orange" if "orange" in tiers else "green"),
        "formats": sorted({r["kind"].split(":", 1)[0] for r in resources}),
        "file_count": len(file_paths),
    }

    payload: dict[str, Any] = {
        "target": target,
        "found": True,
        "summary": summary,
        "resources": resources_section,
        "env_vars": env_section,
        "dependencies": deps_section,
        "recent_changes": recent_changes,
    }

    return _compress_to_budget(payload, token_budget * 4)


def _env_vars_for_files(conn: sqlite3.Connection, file_paths: list[str]) -> dict[str, Any]:
    """Collect env_var defs + env_ref usages for the given files, plus the
    cross-file defs that match the refs' names."""
    if not file_paths:
        return {"defined": [], "referenced": [], "external_definitions": []}

    placeholders = ",".join("?" * len(file_paths))
    defined = conn.execute(
        f"""
        SELECT name, value, file_path FROM entity
        WHERE entity_type IN ('env_var', 'env_def')
          AND file_path IN ({placeholders})
        """,
        file_paths,
    ).fetchall()
    referenced = conn.execute(
        f"""
        SELECT name, file_path FROM entity
        WHERE entity_type IN ('env_ref', 'env_use')
          AND file_path IN ({placeholders})
        """,
        file_paths,
    ).fetchall()

    # For each referenced env, find where it's defined ELSEWHERE in the graph.
    ref_names = sorted({r["name"] for r in referenced})
    external_defs: list[dict[str, Any]] = []
    if ref_names:
        for name in ref_names:
            rows = conn.execute(
                """
                SELECT file_path, value FROM entity
                WHERE entity_type IN ('env_var', 'env_def') AND name = ?
                """,
                (name,),
            ).fetchall()
            for r in rows:
                if r["file_path"] in file_paths:
                    continue  # already in `defined`
                external_defs.append({
                    "name": name,
                    "defined_in": r["file_path"],
                    "value": r["value"],
                })

    return {
        "defined": [{"name": r["name"], "value": r["value"], "file": r["file_path"]}
                    for r in defined],
        "referenced": [{"name": r["name"], "file": r["file_path"]} for r in referenced],
        "external_definitions": external_defs,
    }


def _dependencies_for_resources(
    conn: sqlite3.Connection, resource_uids: list[str],
) -> list[dict[str, Any]]:
    """1-hop dependencies (out-edges) from each resource."""
    if not resource_uids:
        return []
    out: list[dict[str, Any]] = []
    rel_types = ("DEPENDS_ON", "USES", "MOUNTS", "EXPOSES")
    placeholders = ",".join("?" * len(resource_uids))
    rel_placeholders = ",".join("?" * len(rel_types))
    rows = conn.execute(
        f"""
        SELECT src_id, dst_kind, dst_id, rel_type
        FROM edge
        WHERE src_kind = 'resource' AND src_id IN ({placeholders})
          AND rel_type IN ({rel_placeholders})
        """,
        (*resource_uids, *rel_types),
    ).fetchall()
    for r in rows:
        # Enrich destination with its name if it's a resource.
        target_name = r["dst_id"]
        if r["dst_kind"] == "resource":
            res_row = conn.execute(
                "SELECT name, kind FROM resource WHERE uid = ?", (r["dst_id"],),
            ).fetchone()
            if res_row:
                target_name = f"{res_row['kind']} {res_row['name']}"
        out.append({
            "from_uid": r["src_id"],
            "to_kind": r["dst_kind"],
            "to": target_name,
            "rel_type": r["rel_type"],
        })
    return out


def _compress_to_budget(payload: dict[str, Any], char_budget: int) -> dict[str, Any]:
    """Iteratively shrink `payload` until json.dumps fits under char_budget."""
    if char_budget <= 0:
        return payload

    def size(p: dict) -> int:
        return len(json.dumps(p))

    if size(payload) <= char_budget:
        return payload

    # Step 1: drop `properties` from each resource.
    for r in payload.get("resources", []):
        r.pop("properties", None)
    if size(payload) <= char_budget:
        payload.setdefault("_compression", []).append("dropped:properties")
        return payload

    # Step 2: drop env_var values (keep names + files).
    ev = payload.get("env_vars", {})
    for entry in ev.get("defined", []):
        entry.pop("value", None)
    for entry in ev.get("external_definitions", []):
        entry.pop("value", None)
    if size(payload) <= char_budget:
        payload.setdefault("_compression", []).extend(["dropped:properties", "dropped:env_values"])
        return payload

    # Step 3: drop non-production resources.
    payload["resources"] = [
        r for r in payload.get("resources", [])
        if r.get("risk_tier") == "red" or r.get("environment") == "production"
    ] or payload["resources"][:5]
    if size(payload) <= char_budget:
        payload.setdefault("_compression", []).extend(
            ["dropped:properties", "dropped:env_values", "dropped:non_prod_resources"])
        return payload

    # Step 4: truncate lists.
    payload["resources"] = payload.get("resources", [])[:5]
    payload["dependencies"] = payload.get("dependencies", [])[:10]
    payload["recent_changes"] = payload.get("recent_changes", [])[:5]
    if payload.get("env_vars"):
        for k in ("defined", "referenced", "external_definitions"):
            if k in payload["env_vars"]:
                payload["env_vars"][k] = payload["env_vars"][k][:8]

    payload.setdefault("_compression", []).extend([
        "dropped:properties", "dropped:env_values",
        "dropped:non_prod_resources", "truncated:lists",
    ])
    if size(payload) <= char_budget:
        return payload

    # Step 5: nuclear — keep summary + production resources only.
    payload = {
        "target": payload.get("target"),
        "found": payload.get("found"),
        "summary": payload.get("summary"),
        "resources": payload.get("resources", [])[:3],
        "_compression": payload.get("_compression", []) + ["nuclear:summary_only"],
    }
    return payload
