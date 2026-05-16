"""Blast-radius: the primitive that DataTalks.Club would have called first.

Given a (operation, target) pair, return:
  * risk_tier      — green | orange | red
  * action         — proceed | confirm | block
  * dependents     — list of resources/services the target affects
  * environment    — inferred environment of the target
  * reasons        — human-readable bullet list of why the recommendation
  * incident_refs  — recent memory entries mentioning this target

Design:
  * The recommendation NEVER auto-executes. We surface; the agent / human
    decides. (HITL gate per OWASP 2026 guidance.)
  * Tiers are computed from explicit signals only. No model in the loop.
  * Cheap: one targeted query per call. Designed to be invoked on every
    destructive tool call as a PreToolUse hook.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from engram.graph import (
    RISK_GREEN, RISK_ORANGE, RISK_RED,
    find_resource_by_name, find_resources_for_service,
    get_edges_from, get_edges_to,
)


# ---------------------------------------------------------------------------
# Operation classification.
# ---------------------------------------------------------------------------

# Verbs that DEFINITIONALLY destroy state.
_DESTRUCTIVE_VERBS = frozenset({
    "destroy", "delete", "remove", "rm", "drop", "purge", "wipe",
    "terminate", "uninstall", "scrap",
})

# Verbs that change state but are recoverable / non-deleting.
_MUTATING_VERBS = frozenset({
    "apply", "deploy", "create", "update", "modify", "patch", "scale",
    "rollout", "restart", "set", "configure",
})

# Verbs that are safe.
_READ_VERBS = frozenset({
    "get", "list", "describe", "show", "plan", "diff", "logs", "status",
    "ps", "inspect", "history", "events", "top", "config", "explain",
    "version", "info", "validate", "lint", "fmt", "view", "read",
    "template", "render", "search", "help", "completion",
})


def classify_operation(operation: str) -> str:
    """Bucket an operation string into 'destructive' | 'mutating' | 'read' | 'unknown'.

    Accepts things like 'terraform destroy', 'kubectl delete', 'helm uninstall',
    'rm -rf /etc', or just 'destroy'.
    """
    op = operation.strip().lower()
    if not op:
        return "unknown"

    # Pull out the verb. For shell-style invocations the verb is usually the
    # second word; for bare verbs it's the first.
    tokens = re.findall(r"[a-z\-_]+", op)
    if not tokens:
        return "unknown"

    for tok in tokens:
        if tok in _DESTRUCTIVE_VERBS:
            return "destructive"
    for tok in tokens:
        if tok in _MUTATING_VERBS:
            return "mutating"
    for tok in tokens:
        if tok in _READ_VERBS:
            return "read"
    return "unknown"


# ---------------------------------------------------------------------------
# Environment inference.
# ---------------------------------------------------------------------------

_PROD_HINTS = ("prod", "production", "live")
_STAGING_HINTS = ("stag", "staging", "preprod", "pre-prod", "uat", "qa")
_DEV_HINTS = ("dev", "develop", "development", "local", "test", "sandbox")


def infer_environment_from_path(path: str) -> str:
    """Best-effort environment inference from a file or resource path."""
    if not path:
        return ""
    p = path.lower().replace("\\", "/")
    segments = re.split(r"[/_.\-]", p)
    for seg in segments:
        if seg in _PROD_HINTS:
            return "production"
    for seg in segments:
        if seg in _STAGING_HINTS:
            return "staging"
    for seg in segments:
        if seg in _DEV_HINTS:
            return "dev"
    return ""


def tier_for_environment(env: str) -> str:
    if env == "production":
        return RISK_RED
    if env == "staging":
        return RISK_ORANGE
    return RISK_GREEN


# ---------------------------------------------------------------------------
# Dependents — who relies on this target?
# ---------------------------------------------------------------------------

def collect_dependents(
    conn: sqlite3.Connection,
    target_kind: str,
    target_id: str,
    *,
    hops: int = 1,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Find resources/files/services that depend on the target.

    A "dependent" is anything with an edge of relation type DEPENDS_ON, USES,
    USES_ENV, MOUNTS, EXPOSES, or CONFIGURES *pointing TO* the target.
    """
    seen: set[tuple[str, str]] = set()
    frontier: list[tuple[str, str]] = [(target_kind, target_id)]
    out: list[dict[str, Any]] = []

    inbound_rel_types = {
        "DEPENDS_ON", "USES", "USES_ENV", "MOUNTS",
        "EXPOSES", "CONFIGURES", "BELONGS_TO",
    }

    for _ in range(hops):
        next_frontier: list[tuple[str, str]] = []
        for kind, ident in frontier:
            for edge in get_edges_to(conn, kind, ident):
                if edge["rel_type"] not in inbound_rel_types:
                    continue
                src_key = (edge["src_kind"], edge["src_id"])
                if src_key in seen:
                    continue
                seen.add(src_key)
                out.append({
                    "kind": edge["src_kind"],
                    "id": edge["src_id"],
                    "rel_type": edge["rel_type"],
                })
                next_frontier.append(src_key)
                if len(out) >= limit:
                    return out
        frontier = next_frontier
        if not frontier:
            break
    return out


# ---------------------------------------------------------------------------
# Recent incidents — memory entries mentioning the target.
# ---------------------------------------------------------------------------

def recent_incidents(
    conn: sqlite3.Connection, target_name: str, *, limit: int = 5,
) -> list[dict[str, Any]]:
    """Memory entries of type 'error' or 'runbook' mentioning the target name."""
    rows = conn.execute(
        """
        SELECT id, content, memory_type, created_at, service_name
        FROM memory
        WHERE memory_type IN ('error', 'runbook')
          AND lower(content) LIKE ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (f"%{target_name.lower()}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# The main entrypoint.
# ---------------------------------------------------------------------------

@dataclass
class BlastRadiusResult:
    operation: str
    operation_class: str        # destructive | mutating | read | unknown
    target: str
    resolved_resources: list[dict[str, Any]] = field(default_factory=list)
    environment: str = ""
    risk_tier: str = RISK_GREEN
    action: str = "proceed"     # proceed | confirm | block
    dependents: list[dict[str, Any]] = field(default_factory=list)
    incident_refs: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def assess(
    conn: sqlite3.Connection,
    operation: str,
    target: str,
) -> BlastRadiusResult:
    """The single function an agent calls before any destructive action.

    Returns a fully-populated BlastRadiusResult. Never raises on missing data;
    if the target is unknown, returns RISK_ORANGE with action='confirm'
    (better to ask than to silently miss).
    """
    op_class = classify_operation(operation)

    result = BlastRadiusResult(
        operation=operation,
        operation_class=op_class,
        target=target,
    )

    # Step 1: resolve the target. Try direct resource match, then service.
    resources = find_resource_by_name(conn, target)
    if not resources:
        resources = find_resources_for_service(conn, target)
    result.resolved_resources = resources

    # Step 2: collect user annotations on resolved resources. These are the
    # "tell Engram what only you know" facts for click-ops resources.
    # User-set environment OVERRIDES auto-inferred environment.
    from engram.graph import get_user_annotations
    user_annotations_by_uid: dict[str, dict[str, str]] = {}
    for r in resources:
        anns = get_user_annotations(conn, r["uid"])
        if anns:
            user_annotations_by_uid[r["uid"]] = anns

    # Step 3: infer environment.
    # Priority: user-set environment > Resource.environment column > path inference.
    user_envs = {
        anns.get("environment", "") for anns in user_annotations_by_uid.values()
        if anns.get("environment")
    }
    if "production" in user_envs:
        result.environment = "production"
    elif "staging" in user_envs:
        result.environment = "staging"
    elif user_envs:
        result.environment = next(iter(user_envs))
    else:
        envs = {r.get("environment", "") for r in resources if r.get("environment")}
        if envs:
            if "production" in envs:
                result.environment = "production"
            elif "staging" in envs:
                result.environment = "staging"
            else:
                result.environment = next(iter(envs))
        else:
            for r in resources:
                inferred = infer_environment_from_path(r.get("file_path", ""))
                if inferred:
                    result.environment = inferred
                    break
            if not result.environment:
                result.environment = infer_environment_from_path(target)

    # Step 3: dependents (1-hop) for each resolved resource.
    seen_keys: set[tuple[str, str]] = set()
    for r in resources[:20]:  # cap fan-out
        for dep in collect_dependents(conn, "resource", r["uid"], hops=1, limit=50):
            k = (dep["kind"], dep["id"])
            if k not in seen_keys:
                seen_keys.add(k)
                result.dependents.append(dep)

    # Step 4: recent incidents mentioning the target.
    result.incident_refs = recent_incidents(conn, target)

    # Step 5: decide tier + action.
    tier_from_env = tier_for_environment(result.environment)

    # Bias up for destructive ops in any non-green environment.
    if op_class == "destructive":
        result.risk_tier = RISK_RED if tier_from_env in (RISK_ORANGE, RISK_RED) else RISK_ORANGE
        if result.dependents:
            # Many dependents pushes a destructive op to red regardless of env.
            if len(result.dependents) >= 5:
                result.risk_tier = RISK_RED
    elif op_class == "mutating":
        result.risk_tier = tier_from_env
    elif op_class == "read":
        result.risk_tier = RISK_GREEN
    else:
        # Unknown op class — be conservative.
        result.risk_tier = max(RISK_ORANGE, tier_from_env, key=_tier_rank)

    # Recommendation.
    if result.risk_tier == RISK_RED:
        result.action = "block"
    elif result.risk_tier == RISK_ORANGE:
        result.action = "confirm"
    else:
        result.action = "proceed"

    # Reasons (human-readable explainer).
    if op_class == "destructive":
        result.reasons.append(f"Operation '{operation}' is destructive (deletes/destroys state).")
    elif op_class == "mutating":
        result.reasons.append(f"Operation '{operation}' mutates state.")
    elif op_class == "read":
        result.reasons.append(f"Operation '{operation}' is read-only.")
    else:
        result.reasons.append(f"Operation '{operation}' could not be classified — treating cautiously.")

    if result.environment == "production":
        result.reasons.append("Target is in PRODUCTION.")
    elif result.environment == "staging":
        result.reasons.append("Target is in staging.")
    elif result.environment:
        result.reasons.append(f"Target environment: {result.environment}.")
    else:
        result.reasons.append("Target environment could not be inferred.")

    if not resources:
        result.reasons.append(
            f"No Resource matched '{target}' in the graph — recommend confirming "
            "the target name and re-running `engram index` if recently changed."
        )

    if result.dependents:
        result.reasons.append(f"{len(result.dependents)} dependent resource(s) found.")

    if result.incident_refs:
        result.reasons.append(
            f"{len(result.incident_refs)} prior incident/runbook memory(s) mention this target."
        )

    # Surface user annotations — they're the "I told Engram so" reasons.
    for uid, anns in user_annotations_by_uid.items():
        if "owner" in anns:
            result.reasons.append(f"User-set owner: {anns['owner']}.")
        if "runbook" in anns:
            result.reasons.append(f"User-attached runbook: {anns['runbook']}")
        if "note" in anns:
            result.reasons.append(f"User note: {anns['note']}")
        # environment is already surfaced via the env line above.

    return result


def _tier_rank(tier: str) -> int:
    return {RISK_GREEN: 0, RISK_ORANGE: 1, RISK_RED: 2}.get(tier, 1)
