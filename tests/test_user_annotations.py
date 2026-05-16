"""Tests for engram annotate — user-set facts about click-ops resources.

Covers:
  1. Setting an environment annotation makes assess() return red/block
     on an otherwise-untagged resource
  2. Setting owner / runbook / note shows up in assess() reasons
  3. Re-annotating updates rather than duplicating
  4. Removing an annotation removes its effect
  5. annotation_user.UNIQUE (target_uid, key) is enforced
"""

from __future__ import annotations

from engram.graph import (
    FileRow, ProjectRow, ResourceRow,
    get_user_annotations, remove_user_annotation, resource_uid,
    upsert_file, upsert_project, upsert_resource, upsert_user_annotation,
)
from engram.safety.blast_radius import assess


def _seed_clickops_rds(conn, *, name: str = "payments-untagged"):
    """A click-ops RDS: no environment tag, no risk tier inference signal."""
    upsert_project(conn, ProjectRow(path="", name="cloud", project_type="cloud"))
    file_path = "aws://1/us-east-1/rds"
    upsert_file(conn, FileRow(
        path=file_path, project_path="",
        name="aws:rds", extension="",
        size_bytes=0, content_hash="aws:1:us-east-1:rds",
        modified_at="2026-05-16T00:00:00Z",
        risk_tier="green",
    ))
    uid = resource_uid("aws:rds:DBInstance", name, "us-east-1", file_path)
    upsert_resource(conn, ResourceRow(
        uid=uid, file_path=file_path, kind="aws:rds:DBInstance",
        name=name, namespace="us-east-1",
        environment="",   # ← deliberately empty (the click-ops scenario)
        risk_tier="green",
        properties={"discovered_from": "aws-cli"},
    ))
    return uid


def test_environment_annotation_promotes_to_red(tmp_db):
    """Without annotation: green/proceed. With annotation env=prod: red/block."""
    uid = _seed_clickops_rds(tmp_db)

    # Baseline — destructive op on untagged resource still goes to orange
    # (destructive bias) but NOT red.
    r1 = assess(tmp_db, "terraform destroy", "payments-untagged")
    assert r1.risk_tier == "orange"
    assert r1.action == "confirm"

    # Annotate as production.
    upsert_user_annotation(tmp_db, target_uid=uid, key="environment", value="production")

    r2 = assess(tmp_db, "terraform destroy", "payments-untagged")
    assert r2.environment == "production"
    assert r2.risk_tier == "red"
    assert r2.action == "block"
    assert any("PRODUCTION" in r for r in r2.reasons)


def test_owner_runbook_note_surface_in_reasons(tmp_db):
    uid = _seed_clickops_rds(tmp_db)
    upsert_user_annotation(tmp_db, target_uid=uid, key="environment", value="production")
    upsert_user_annotation(tmp_db, target_uid=uid, key="owner", value="platform-team")
    upsert_user_annotation(tmp_db, target_uid=uid, key="runbook",
                           value="https://example.com/runbook/payments-db")
    upsert_user_annotation(tmp_db, target_uid=uid, key="note",
                           value="stores PII; legal must approve any destroy")

    r = assess(tmp_db, "terraform destroy", "payments-untagged")
    joined = " ".join(r.reasons)
    assert "platform-team" in joined
    assert "https://example.com/runbook/payments-db" in joined
    assert "PII" in joined


def test_reannotating_updates_in_place(tmp_db):
    uid = _seed_clickops_rds(tmp_db)
    upsert_user_annotation(tmp_db, target_uid=uid, key="environment", value="dev")
    upsert_user_annotation(tmp_db, target_uid=uid, key="environment", value="production")
    upsert_user_annotation(tmp_db, target_uid=uid, key="environment", value="staging")

    # Only the latest value should remain.
    anns = get_user_annotations(tmp_db, uid)
    assert anns == {"environment": "staging"}

    # Row count check.
    n = tmp_db.execute(
        "SELECT count(*) FROM annotation_user WHERE target_uid = ?", (uid,)
    ).fetchone()[0]
    assert n == 1


def test_remove_annotation_reverts_effect(tmp_db):
    uid = _seed_clickops_rds(tmp_db)
    upsert_user_annotation(tmp_db, target_uid=uid, key="environment", value="production")

    r1 = assess(tmp_db, "terraform destroy", "payments-untagged")
    assert r1.risk_tier == "red"

    remove_user_annotation(tmp_db, uid, "environment")

    r2 = assess(tmp_db, "terraform destroy", "payments-untagged")
    # Back to orange (no env signal).
    assert r2.risk_tier == "orange"


def test_remove_all_annotations(tmp_db):
    uid = _seed_clickops_rds(tmp_db)
    upsert_user_annotation(tmp_db, target_uid=uid, key="environment", value="production")
    upsert_user_annotation(tmp_db, target_uid=uid, key="owner", value="alice")
    upsert_user_annotation(tmp_db, target_uid=uid, key="runbook", value="https://x")

    n = remove_user_annotation(tmp_db, uid, None)  # None = remove all
    assert n == 3
    assert get_user_annotations(tmp_db, uid) == {}


def test_user_annotation_overrides_source_environment(tmp_db):
    """If the resource was discovered with env=staging from a tag but the user
    annotates env=production, the user wins. They know things tags don't."""
    upsert_project(tmp_db, ProjectRow(path="", name="cloud"))
    fp = "aws://2/us-east-1/rds"
    upsert_file(tmp_db, FileRow(
        path=fp, project_path="", name="aws:rds", extension="",
        size_bytes=0, content_hash="x", modified_at="2026-05-16T00:00:00Z",
        risk_tier="orange",
    ))
    uid = resource_uid("aws:rds:DBInstance", "shared-db", "us-east-1", fp)
    upsert_resource(tmp_db, ResourceRow(
        uid=uid, file_path=fp, kind="aws:rds:DBInstance",
        name="shared-db", namespace="us-east-1",
        environment="staging",  # tag said staging
        risk_tier="orange",
        properties={"discovered_from": "aws-cli"},
    ))
    upsert_user_annotation(tmp_db, target_uid=uid, key="environment", value="production")

    r = assess(tmp_db, "terraform destroy", "shared-db")
    assert r.environment == "production"
    assert r.risk_tier == "red"
    assert r.action == "block"
