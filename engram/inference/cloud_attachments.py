"""Post-import pass that wires cross-resource USES edges from the cloud graph.

When `_import_rds` / `_import_ec2` / `_import_lambda` etc. run, they capture
the attachment IDs (security_group_ids, subnet_ids, iam role ARN, vpc_id) in
each resource's `properties._attachments` dict. They cannot resolve those
to UIDs at import time because the related resources may not have been
imported yet.

This pass runs *after* all cloud imports complete:
  1. Build a lookup from every AWS-native ID (sg-abc, subnet-xyz, vpc-foo,
     i-abc, arn:aws:iam:...:role/foo, etc.) to the corresponding resource UID.
  2. Walk every Resource with a `_attachments` block and emit USES edges to
     each resolved ID.

Edges are idempotent (UNIQUE constraint on src/dst/rel_type). Edges carry
`properties.inferred_from = 'cloud_attachment'` so they're distinguishable
from value_match-inferred edges and from source-IaC-declared edges.

The result: after `engram import-cloud --provider aws --kinds rds,ec2,sg,subnet,iam`,
asking `engram dependents_of sg-abc123` returns every RDS and EC2 instance
that uses that SG. THIS is what makes blast-radius for network deletes
work.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from engram.graph import EdgeSpec, upsert_edge

logger = logging.getLogger(__name__)


# AWS-native ID prefixes we know how to resolve. Each maps a stored
# property key to a resolution strategy.
_RESOLVABLE_ID_PROPS = (
    "group_id",         # SecurityGroup
    "subnet_id",        # Subnet
    "vpc_id",           # VPC
    "instance_id",      # EC2 Instance
    "arn",              # any ARN-bearing resource
    "function_arn",     # Lambda
    "topic_arn",        # SNS
    "queue_url",        # SQS
    "stream_arn",       # Kinesis
    "table_arn",        # DynamoDB
)


@dataclass
class AttachmentStats:
    resources_with_attachments: int = 0
    edges_inferred: int = 0
    unresolved: int = 0


def link_cloud_attachments(conn: sqlite3.Connection) -> AttachmentStats:
    """Walk every Resource's `_attachments` block, emit USES edges."""
    stats = AttachmentStats()

    # Step 1: id-lookup table.
    # For each Resource, scan its properties for resolvable IDs and index
    # them. Multiple resources can share an ID (rare), so we store a list.
    id_to_uid: dict[str, str] = {}
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
        for key in _RESOLVABLE_ID_PROPS:
            v = props.get(key)
            if isinstance(v, str) and v:
                id_to_uid[v] = r["uid"]

    # Step 2: walk Resources that have an _attachments block.
    # We DON'T early-return when id_to_uid is empty — we still want to
    # count unresolved attachments so the user knows the linkage failed.
    rows = conn.execute(
        "SELECT uid, kind, name, properties FROM resource "
        "WHERE properties LIKE '%_attachments%'"
    ).fetchall()

    for r in rows:
        try:
            props = json.loads(r["properties"]) if isinstance(r["properties"], str) else r["properties"]
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(props, dict):
            continue
        attachments = props.get("_attachments")
        if not isinstance(attachments, dict):
            continue
        stats.resources_with_attachments += 1

        src_uid = r["uid"]

        # Collect every ID we should try to resolve.
        ids_to_resolve: list[tuple[str, str]] = []  # (id, attachment_field)

        # Lists.
        for field in ("security_group_ids", "subnet_ids", "vpc_ids"):
            vals = attachments.get(field) or []
            if isinstance(vals, list):
                for v in vals:
                    if isinstance(v, str) and v:
                        ids_to_resolve.append((v, field))

        # Single-value ARN-ish fields.
        for field in ("iam_instance_profile_arn", "role_arn", "execution_role_arn",
                      "kms_key_arn", "target_arn"):
            v = attachments.get(field)
            if isinstance(v, str) and v:
                ids_to_resolve.append((v, field))

        for attach_id, attach_field in ids_to_resolve:
            dst_uid = id_to_uid.get(attach_id)
            if not dst_uid:
                # The attached resource hasn't been imported yet; that's the
                # main mode of failure here. Caller usually imports `sg` /
                # `subnet` / `iam` alongside `rds` / `ec2` so this is rare.
                stats.unresolved += 1
                continue
            if dst_uid == src_uid:
                continue  # self-reference
            upsert_edge(conn, EdgeSpec(
                src_kind="resource", src_id=src_uid,
                dst_kind="resource", dst_id=dst_uid,
                rel_type="USES",
                properties={
                    "inferred_from": "cloud_attachment",
                    "attachment_field": attach_field,
                    "attachment_id": attach_id,
                },
            ))
            stats.edges_inferred += 1

    logger.info(
        "cloud-attachment inference: %d edges inferred from %d resources "
        "(%d unresolved)",
        stats.edges_inferred, stats.resources_with_attachments, stats.unresolved,
    )
    return stats
