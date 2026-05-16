"""Tests for engram drift — the CTO-demo command."""

from __future__ import annotations

import json

from engram.drift import detect_drift, render_drift
from engram.graph import (
    EdgeSpec, EntityRow, FileRow, ProjectRow, ResourceRow,
    entity_uid, resource_uid,
    upsert_edge, upsert_entity, upsert_file, upsert_project, upsert_resource,
)


def _seed_iac_resource(conn, name: str, kind: str = "tf:aws_db_instance",
                        env: str = "", file_path: str = "/repo/main.tf") -> str:
    upsert_project(conn, ProjectRow(path="/repo", name="repo"))
    upsert_file(conn, FileRow(
        path=file_path, project_path="/repo",
        name=file_path.rsplit("/", 1)[-1], extension=".tf",
        size_bytes=10, content_hash=f"h-{name}",
        modified_at="2026-05-17T00:00:00Z",
        risk_tier="red" if env == "production" else "green",
    ))
    uid = resource_uid(kind, name, "", file_path)
    upsert_resource(conn, ResourceRow(
        uid=uid, file_path=file_path, kind=kind, name=name,
        environment=env, risk_tier="red" if env == "production" else "green",
        properties={"engine": "postgres"},  # NO discovered_from
    ))
    return uid


def _seed_cloud_resource(conn, name: str, kind: str = "aws:rds:DBInstance",
                         env: str = "", region: str = "us-east-1") -> str:
    fp = f"aws://1/{region}/{kind.split(':')[1]}"
    upsert_file(conn, FileRow(
        path=fp, project_path="", name=kind.split(":")[1], extension="",
        size_bytes=0, content_hash=fp,
        modified_at="2026-05-17T00:00:00Z",
        risk_tier="red" if env == "production" else "green",
    ))
    uid = resource_uid(kind, name, region, fp)
    upsert_resource(conn, ResourceRow(
        uid=uid, file_path=fp, kind=kind, name=name,
        namespace=region, environment=env,
        risk_tier="red" if env == "production" else "green",
        properties={"discovered_from": "aws-cli", "account": "1", "region": region},
    ))
    return uid


# ---------------------------------------------------------------------------
# Empty / baseline cases
# ---------------------------------------------------------------------------

def test_drift_empty_db(tmp_db):
    report = detect_drift(tmp_db)
    assert report.untracked_cloud == []
    assert report.stale_iac == []
    out = render_drift(report)
    assert "untracked_cloud" not in out  # rendered, no machine-readable key
    assert "ENGRAM DRIFT REPORT" in out


def test_drift_only_iac_no_cloud(tmp_db):
    """All IaC, no cloud-discovered → IaC counts as stale."""
    _seed_iac_resource(tmp_db, "prod-db", env="production")
    report = detect_drift(tmp_db)
    assert report.untracked_cloud == []
    # The IaC resource has no matching cloud presence → stale.
    assert len(report.stale_iac) == 1
    assert report.stale_iac[0]["name"] == "prod-db"


# ---------------------------------------------------------------------------
# Click-ops detection
# ---------------------------------------------------------------------------

def test_drift_finds_untracked_prod_resource(tmp_db):
    """A cloud-discovered prod RDS with no IaC presence is untracked."""
    _seed_cloud_resource(tmp_db, "legacy-prod-2018", env="production")
    report = detect_drift(tmp_db)
    assert len(report.untracked_cloud) == 1
    assert report.untracked_cloud[0]["name"] == "legacy-prod-2018"
    assert report.untracked_cloud[0]["environment"] == "production"
    assert report.untracked_prod_count == 1


def test_drift_iac_match_clears_untracked(tmp_db):
    """If both an IaC tf:aws_db_instance and a cloud aws:rds:DBInstance share
    the same name → tracked, not in the untracked list."""
    _seed_iac_resource(tmp_db, "shared-db", env="production")
    _seed_cloud_resource(tmp_db, "shared-db", env="production")
    report = detect_drift(tmp_db)
    assert report.untracked_cloud == []
    assert report.stale_iac == []


def test_drift_value_match_edge_clears_untracked(tmp_db):
    """If an env-var → cloud-resource edge exists, the cloud resource is
    considered tracked (someone in source code references it by endpoint)."""
    cloud_uid = _seed_cloud_resource(tmp_db, "discovered-via-env-var",
                                      env="production")
    # Seed a file + entity + edge to simulate value_match having run.
    upsert_project(tmp_db, ProjectRow(path="/repo", name="repo"))
    upsert_file(tmp_db, FileRow(
        path="/repo/.env", project_path="/repo",
        name=".env", extension=".env",
        size_bytes=10, content_hash="env-h",
        modified_at="2026-05-17T00:00:00Z",
    ))
    upsert_edge(tmp_db, EdgeSpec(
        src_kind="file", src_id="/repo/.env",
        dst_kind="resource", dst_id=cloud_uid,
        rel_type="DEPENDS_ON",
        properties={"inferred_from": "value_match"},
    ))
    report = detect_drift(tmp_db)
    # Cloud resource has an inbound edge from a file → not "untracked".
    assert report.untracked_cloud == []


# ---------------------------------------------------------------------------
# Stale IaC detection
# ---------------------------------------------------------------------------

def test_drift_iac_without_cloud_is_stale(tmp_db):
    """IaC-defined resource with no matching cloud resource → stale."""
    _seed_iac_resource(tmp_db, "decommissioned-db", env="production")
    _seed_cloud_resource(tmp_db, "different-db", env="production")
    report = detect_drift(tmp_db)
    assert len(report.stale_iac) == 1
    assert report.stale_iac[0]["name"] == "decommissioned-db"


def test_drift_skips_module_and_values_kinds(tmp_db):
    """tf:module, helm:values, yaml:document are organizational, not real
    deployable resources — should not appear in stale_iac."""
    _seed_iac_resource(tmp_db, "some-module", kind="tf:module")
    _seed_iac_resource(tmp_db, "vals", kind="helm:values")
    _seed_iac_resource(tmp_db, "yamldoc", kind="yaml:document")
    report = detect_drift(tmp_db)
    assert report.stale_iac == []


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def test_render_drift_highlights_production(tmp_db):
    _seed_cloud_resource(tmp_db, "leak-prod", env="production")
    _seed_cloud_resource(tmp_db, "dev-cache", env="dev")
    report = detect_drift(tmp_db)
    text = render_drift(report)
    # Prod should be highlighted (❗) and appear before the dev one.
    prod_pos = text.find("leak-prod")
    dev_pos = text.find("dev-cache")
    assert prod_pos != -1
    assert dev_pos != -1
    assert prod_pos < dev_pos
    assert "❗" in text


def test_render_drift_summary_counts_correct(tmp_db):
    _seed_cloud_resource(tmp_db, "a", env="production")
    _seed_cloud_resource(tmp_db, "b", env="staging")
    _seed_cloud_resource(tmp_db, "c", env="dev")
    _seed_iac_resource(tmp_db, "stale", env="production")
    report = detect_drift(tmp_db)
    text = render_drift(report)
    assert "3 cloud resources have no matching IaC" in text
    assert "1    of those are tagged or named as production" in text or \
           "1" in text.split("of those are tagged")[0]
    assert "1 IaC-declared resources have no matching" in text


def test_drift_max_rows_truncation(tmp_db):
    for i in range(50):
        _seed_cloud_resource(tmp_db, f"resource-{i}", env="dev")
    report = detect_drift(tmp_db)
    text = render_drift(report, max_rows=10)
    assert "and 40 more" in text
