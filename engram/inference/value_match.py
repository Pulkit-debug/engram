"""Value-match inference: auto-link env-var values to cloud resources.

The click-ops killer.

When the crawler indexes a source file containing
`DATABASE_URL=postgres://prod-db.cluster-x.us-east-1.rds.amazonaws.com:5432/foo`,
AND `engram import-cloud --provider aws` has discovered an RDS instance
whose `endpoint` property contains `prod-db.cluster-x.us-east-1.rds.amazonaws.com`,
this module creates a DEPENDS_ON edge from the *file* (or its enclosing
project's service) to the cloud Resource — *without any IaC linking them*.

This is what makes Engram work for the median company that has zero
discipline about Terraform but does have env vars that point at real AWS
resources.

Match strategy (deterministic, no LLM):

  1. Walk every Entity where entity_type in {'env_var', 'env_ref',
     'env_def', 'secret_ref', 'config_value'} and the value is non-empty.
  2. For each, try to extract a *candidate hostname/ARN/bucket* by URL
     parsing and ARN regex.
  3. Index every Resource's matchable fields:
       endpoint, dns_name, arn, url, bucket_name, address, host
     into a lookup table keyed by canonical form.
  4. For each entity candidate, check if any Resource's matchable value
     is a strict substring of the candidate (or vice versa, depending on
     shape).
  5. Score by match-length / candidate-length. Threshold at 0.6 to avoid
     spurious hits like "redis" matching "redis-cluster-1.example.com".
  6. Emit edge: src=(file, file_path) → dst=(resource, uid),
     rel_type=DEPENDS_ON, properties={inferred_from: 'value_match',
     match_score: ..., match_field: 'endpoint'}.

The edge is idempotent: re-running upserts (no duplicates).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from engram.graph import EdgeSpec, upsert_edge

logger = logging.getLogger(__name__)


# Entity types whose `value` field might contain a hostname/ARN/bucket reference.
_VALUE_BEARING_ENTITY_TYPES = (
    "env_var", "env_def", "env_ref", "secret_ref",
    "config_value", "variable",
)

# Resource property keys we'll scan for matchable strings.
_MATCHABLE_PROPERTY_KEYS = (
    "endpoint", "dns_name", "arn", "url", "bucket_name", "address",
    "host", "queue_url", "topic_arn", "function_arn", "domain",
)

# Minimum match score (match_len / candidate_len) to accept.
_DEFAULT_MIN_SCORE = 0.6

# Minimum hostname length (don't match "ip" or "db" alone).
_MIN_TOKEN_LEN = 6

# An ARN looks like: arn:aws:rds:us-east-1:123456789012:db:prod-db
_ARN_RE = re.compile(r"\b(arn:[a-z0-9\-]+:[a-z0-9\-]+:[a-z0-9\-]*:[0-9]*:[A-Za-z0-9\-_/.:]+)\b")

# A FQDN-ish hostname inside a string.
_HOSTNAME_RE = re.compile(
    r"\b([a-z0-9][a-z0-9\-]*(?:\.[a-z0-9][a-z0-9\-]*){2,})\b",
    re.IGNORECASE,
)


@dataclass
class ValueMatchStats:
    entities_scanned: int = 0
    resources_indexed: int = 0
    candidates_extracted: int = 0
    edges_inferred: int = 0
    skipped_too_short: int = 0
    skipped_low_score: int = 0


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def infer_value_matches(
    conn: sqlite3.Connection,
    *,
    min_score: float = _DEFAULT_MIN_SCORE,
) -> ValueMatchStats:
    """Walk the graph and emit inferred DEPENDS_ON edges. Idempotent.

    Returns the stats. The connection's autocommit is fine; each upsert
    is its own statement.
    """
    stats = ValueMatchStats()

    # Step 1: build the resource lookup index.
    # We index every (canonical_match_value, resource_uid, field_name) tuple
    # so we can do an O(1) check per candidate.
    lookup: dict[str, list[tuple[str, str]]] = {}
    rows = conn.execute(
        "SELECT uid, properties FROM resource WHERE properties != '' AND properties != '{}'"
    ).fetchall()
    for r in rows:
        try:
            props = json.loads(r["properties"]) if isinstance(r["properties"], str) else r["properties"]
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(props, dict):
            continue
        for key in _MATCHABLE_PROPERTY_KEYS:
            v = props.get(key, "")
            if not v or not isinstance(v, str):
                continue
            canonical = _canonicalize(v)
            if not canonical or len(canonical) < _MIN_TOKEN_LEN:
                continue
            lookup.setdefault(canonical, []).append((r["uid"], key))
            stats.resources_indexed += 1

    if not lookup:
        return stats

    # Step 2: walk value-bearing entities.
    placeholders = ",".join("?" * len(_VALUE_BEARING_ENTITY_TYPES))
    ent_rows = conn.execute(
        f"""
        SELECT e.uid, e.name, e.entity_type, e.value, e.file_path
        FROM entity e
        WHERE e.entity_type IN ({placeholders})
          AND e.value != ''
        """,
        _VALUE_BEARING_ENTITY_TYPES,
    ).fetchall()

    for ent in ent_rows:
        stats.entities_scanned += 1
        value = ent["value"]
        if not value or len(value) < _MIN_TOKEN_LEN:
            stats.skipped_too_short += 1
            continue

        # Extract candidate hostnames + ARNs from the value.
        candidates = _extract_candidates(value)
        if not candidates:
            continue
        stats.candidates_extracted += len(candidates)

        # For each candidate, check the lookup index.
        for cand in candidates:
            cand_canonical = _canonicalize(cand)
            if not cand_canonical:
                continue

            # Exact-match path first (most common case for endpoints / ARNs).
            if cand_canonical in lookup:
                for resource_uid, field_name in lookup[cand_canonical]:
                    _emit_edge(
                        conn, ent, resource_uid,
                        field_name=field_name,
                        match_score=1.0,
                        match_value=cand_canonical,
                    )
                    stats.edges_inferred += 1
                continue

            # Substring-match path: a resource's matchable value is a substring
            # of the candidate (or vice versa).
            best_uid: str | None = None
            best_field: str = ""
            best_score: float = 0.0
            for indexed_value, hits in lookup.items():
                score = _substring_score(cand_canonical, indexed_value)
                if score > best_score:
                    best_score = score
                    best_uid, best_field = hits[0]

            if best_score >= min_score and best_uid:
                _emit_edge(
                    conn, ent, best_uid,
                    field_name=best_field,
                    match_score=round(best_score, 3),
                    match_value=cand_canonical,
                )
                stats.edges_inferred += 1
            elif best_score > 0:
                stats.skipped_low_score += 1

    return stats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _canonicalize(s: str) -> str:
    """Lower-case, strip leading/trailing punctuation, drop port."""
    s = s.strip().lower()
    # Drop scheme.
    if "://" in s:
        s = s.split("://", 1)[1]
    # Drop user-info.
    if "@" in s and "/" not in s.split("@")[0]:
        s = s.split("@", 1)[1]
    # Drop port.
    if ":" in s and not s.startswith("arn:"):
        host, _, rest = s.partition(":")
        # If `rest` is all digits, it's a port — drop it.
        if rest.split("/")[0].isdigit():
            s = host + ("/" + rest.split("/", 1)[1] if "/" in rest else "")
    # Drop path / query.
    s = s.split("/", 1)[0] if "://" not in s and not s.startswith("arn:") else s
    return s.strip("\"'.-_")


def _extract_candidates(value: str) -> list[str]:
    """Pull plausible hostname/ARN/URL candidates out of an env-var value."""
    out: list[str] = []

    # ARNs first (more specific).
    for m in _ARN_RE.findall(value):
        out.append(m)

    # URL-shaped values.
    if "://" in value:
        try:
            parsed = urlparse(value)
            if parsed.hostname:
                out.append(parsed.hostname)
        except ValueError:
            pass

    # Comma-/semicolon-separated values (REDIS_NODES=host1.x,host2.x).
    for token in re.split(r"[,;\s]+", value):
        token = token.strip()
        if not token:
            continue
        if "://" in token:
            try:
                parsed = urlparse(token)
                if parsed.hostname:
                    out.append(parsed.hostname)
                    continue
            except ValueError:
                pass
        # Bare hostname.
        if _HOSTNAME_RE.fullmatch(token):
            out.append(token)
        else:
            # Look inside the token for an FQDN.
            m = _HOSTNAME_RE.search(token)
            if m:
                out.append(m.group(1))

    # Also scan the whole value for embedded FQDNs we missed.
    for m in _HOSTNAME_RE.findall(value):
        if m not in out:
            out.append(m)

    # Dedupe preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for c in out:
        if c.lower() not in seen:
            seen.add(c.lower())
            deduped.append(c)
    return deduped


def _substring_score(candidate: str, indexed: str) -> float:
    """Return a 0..1 score for how strongly the candidate matches the indexed
    value. Score = matched length / max(candidate length, indexed length).
    """
    if not candidate or not indexed:
        return 0.0
    if candidate == indexed:
        return 1.0
    if candidate in indexed:
        return len(candidate) / len(indexed)
    if indexed in candidate:
        return len(indexed) / len(candidate)
    return 0.0


def _emit_edge(
    conn: sqlite3.Connection,
    entity_row: Any,
    resource_uid: str,
    *,
    field_name: str,
    match_score: float,
    match_value: str,
) -> None:
    """Insert a DEPENDS_ON edge from the entity's file to the cloud resource."""
    upsert_edge(conn, EdgeSpec(
        src_kind="file",
        src_id=entity_row["file_path"],
        dst_kind="resource",
        dst_id=resource_uid,
        rel_type="DEPENDS_ON",
        properties={
            "inferred_from": "value_match",
            "via_entity": entity_row["name"],
            "via_entity_type": entity_row["entity_type"],
            "match_field": field_name,
            "match_score": match_score,
            "match_value": match_value,
        },
    ))
