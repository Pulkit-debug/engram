"""Tests for cross-resource USES edges inferred from cloud import attachments.

These are the network/IAM edges that make `blast_radius` aware of "if I
delete this security group, what services lose connectivity?"
"""

from __future__ import annotations

import json

import pytest

from engram.graph import (
    FileRow, ProjectRow, ResourceRow,
    resource_uid, upsert_file, upsert_project, upsert_resource,
)
from engram.inference.cloud_attachments import link_cloud_attachments


def _seed_sg(conn, *, group_id: str, name: str):
    upsert_project(conn, ProjectRow(path="", name="cloud"))
    fp = f"aws://1/us-east-1/sg"
    upsert_file(conn, FileRow(
        path=fp, project_path="", name="sg", extension="",
        size_bytes=0, content_hash=fp,
        modified_at="2026-05-17T00:00:00Z",
    ))
    uid = resource_uid("aws:ec2:SecurityGroup", name, "us-east-1", fp)
    upsert_resource(conn, ResourceRow(
        uid=uid, file_path=fp, kind="aws:ec2:SecurityGroup",
        name=name, namespace="us-east-1",
        properties={"group_id": group_id, "discovered_from": "aws-cli"},
    ))
    return uid


def _seed_subnet(conn, *, subnet_id: str, name: str):
    fp = f"aws://1/us-east-1/subnet"
    upsert_file(conn, FileRow(
        path=fp, project_path="", name="subnet", extension="",
        size_bytes=0, content_hash=fp,
        modified_at="2026-05-17T00:00:00Z",
    ))
    uid = resource_uid("aws:ec2:Subnet", name, "us-east-1", fp)
    upsert_resource(conn, ResourceRow(
        uid=uid, file_path=fp, kind="aws:ec2:Subnet",
        name=name, namespace="us-east-1",
        properties={"subnet_id": subnet_id, "discovered_from": "aws-cli"},
    ))
    return uid


def _seed_rds_with_attachments(conn, *, name: str, sg_ids: list[str],
                                subnet_ids: list[str]):
    fp = "aws://1/us-east-1/rds"
    upsert_file(conn, FileRow(
        path=fp, project_path="", name="rds", extension="",
        size_bytes=0, content_hash=fp,
        modified_at="2026-05-17T00:00:00Z",
    ))
    uid = resource_uid("aws:rds:DBInstance", name, "us-east-1", fp)
    upsert_resource(conn, ResourceRow(
        uid=uid, file_path=fp, kind="aws:rds:DBInstance",
        name=name, namespace="us-east-1",
        properties={
            "discovered_from": "aws-cli",
            "_attachments": {
                "security_group_ids": sg_ids,
                "subnet_ids": subnet_ids,
            },
        },
    ))
    return uid


def _edges_from(conn, src_uid: str) -> list[dict]:
    return [{k: r[k] for k in r.keys()}
            for r in conn.execute(
                "SELECT * FROM edge WHERE src_kind='resource' AND src_id=?",
                (src_uid,),
            ).fetchall()]


# ---------------------------------------------------------------------------
# Core scenarios
# ---------------------------------------------------------------------------

def test_rds_to_sg_edge_created(tmp_db):
    sg_uid = _seed_sg(tmp_db, group_id="sg-abc123", name="payments-sg")
    rds_uid = _seed_rds_with_attachments(
        tmp_db, name="payments-prod",
        sg_ids=["sg-abc123"], subnet_ids=[],
    )
    stats = link_cloud_attachments(tmp_db)
    assert stats.edges_inferred == 1

    edges = _edges_from(tmp_db, rds_uid)
    assert len(edges) == 1
    assert edges[0]["dst_kind"] == "resource"
    assert edges[0]["dst_id"] == sg_uid
    assert edges[0]["rel_type"] == "USES"
    props = json.loads(edges[0]["properties"])
    assert props["inferred_from"] == "cloud_attachment"
    assert props["attachment_field"] == "security_group_ids"


def test_rds_to_multiple_sgs(tmp_db):
    sg1 = _seed_sg(tmp_db, group_id="sg-a", name="sg-a")
    sg2 = _seed_sg(tmp_db, group_id="sg-b", name="sg-b")
    rds_uid = _seed_rds_with_attachments(
        tmp_db, name="db", sg_ids=["sg-a", "sg-b"], subnet_ids=[],
    )
    stats = link_cloud_attachments(tmp_db)
    assert stats.edges_inferred == 2
    edge_dsts = {e["dst_id"] for e in _edges_from(tmp_db, rds_uid)}
    assert edge_dsts == {sg1, sg2}


def test_rds_to_subnets(tmp_db):
    sn = _seed_subnet(tmp_db, subnet_id="subnet-xyz", name="prod-private-1a")
    rds_uid = _seed_rds_with_attachments(
        tmp_db, name="db", sg_ids=[], subnet_ids=["subnet-xyz"],
    )
    stats = link_cloud_attachments(tmp_db)
    assert stats.edges_inferred == 1
    edges = _edges_from(tmp_db, rds_uid)
    assert edges[0]["dst_id"] == sn
    props = json.loads(edges[0]["properties"])
    assert props["attachment_field"] == "subnet_ids"


def test_unresolved_attachment_does_not_crash(tmp_db):
    """If the SG isn't in the graph yet, we just count it as unresolved."""
    _seed_rds_with_attachments(
        tmp_db, name="db", sg_ids=["sg-nonexistent"], subnet_ids=[],
    )
    stats = link_cloud_attachments(tmp_db)
    assert stats.edges_inferred == 0
    assert stats.unresolved == 1


def test_idempotent_relink(tmp_db):
    _seed_sg(tmp_db, group_id="sg-a", name="sg-a")
    _seed_rds_with_attachments(
        tmp_db, name="db", sg_ids=["sg-a"], subnet_ids=[],
    )
    link_cloud_attachments(tmp_db)
    link_cloud_attachments(tmp_db)
    link_cloud_attachments(tmp_db)
    # 3 runs, 1 edge — UNIQUE constraint on (src, dst, rel_type) is honored.
    count = tmp_db.execute(
        "SELECT count(*) FROM edge WHERE src_kind='resource' AND rel_type='USES'"
    ).fetchone()[0]
    assert count == 1


def test_dependents_of_sg_finds_rds(tmp_db):
    """End-to-end: after linking, `dependents_of` the SG finds the RDS."""
    from engram.safety.blast_radius import collect_dependents

    sg_uid = _seed_sg(tmp_db, group_id="sg-prod", name="payments-prod-sg")
    rds_uid = _seed_rds_with_attachments(
        tmp_db, name="payments-prod",
        sg_ids=["sg-prod"], subnet_ids=[],
    )
    link_cloud_attachments(tmp_db)

    deps = collect_dependents(tmp_db, "resource", sg_uid, hops=1)
    # The RDS depends on the SG (uses it) → it's a dependent.
    dep_ids = {d["id"] for d in deps}
    assert rds_uid in dep_ids
