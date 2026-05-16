"""Tests for engram.inference.value_match — the pioneer feature.

Scenarios covered:
  1. Env var holding a postgres URL with an RDS endpoint → exact match → edge created
  2. Env var holding just a hostname → substring match → edge created
  3. Env var holding an ARN → match → edge created
  4. Env var with a hostname *unrelated* to any resource → no edge
  5. Env var too short (no real hostname) → no edge
  6. Two resources with similar-but-distinct endpoints → matches to the right one
  7. Re-running the inference is idempotent (no duplicate edges)
  8. End-to-end: blast_radius surfaces inferred dependencies as dependents
"""

from __future__ import annotations

import json

from engram.graph import (
    EntityRow, FileRow, ProjectRow, ResourceRow,
    entity_uid, resource_uid, upsert_entity, upsert_file,
    upsert_project, upsert_resource,
)
from engram.inference.value_match import infer_value_matches


def _seed_file_and_project(conn, file_path: str = "/repo/app/.env"):
    upsert_project(conn, ProjectRow(path="/repo", name="app"))
    upsert_file(conn, FileRow(
        path=file_path, project_path="/repo",
        name=".env", extension=".env",
        size_bytes=100, content_hash="h",
        modified_at="2026-05-16T00:00:00Z",
    ))


def _seed_rds(conn, *, name: str, endpoint: str, env: str = "production",
              file_path: str = "aws://1/us-east-1/rds") -> str:
    upsert_file(conn, FileRow(
        path=file_path, project_path="",
        name="aws:rds", extension="",
        size_bytes=0, content_hash="aws:1:us-east-1:rds",
        modified_at="2026-05-16T00:00:00Z",
        risk_tier="red" if env == "production" else "green",
    ))
    uid = resource_uid("aws:rds:DBInstance", name, "us-east-1", file_path)
    upsert_resource(conn, ResourceRow(
        uid=uid, file_path=file_path, kind="aws:rds:DBInstance",
        name=name, namespace="us-east-1", environment=env,
        risk_tier="red" if env == "production" else "green",
        properties={"endpoint": endpoint, "discovered_from": "aws-cli"},
    ))
    return uid


def _seed_env(conn, *, name: str, value: str,
              file_path: str = "/repo/app/.env"):
    upsert_entity(conn, EntityRow(
        uid=entity_uid(name, "env_var", file_path),
        file_path=file_path, name=name, entity_type="env_var", value=value,
    ))


def _edges_from_file(conn, file_path: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM edge WHERE src_kind='file' AND src_id=?",
        (file_path,),
    ).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


# ---------------------------------------------------------------------------
# 1. Exact match (URL contains the full endpoint)
# ---------------------------------------------------------------------------

def test_postgres_url_exact_match_creates_edge(tmp_db):
    _seed_file_and_project(tmp_db)
    rds_uid = _seed_rds(
        tmp_db,
        name="datatalks-prod-db",
        endpoint="datatalks-prod-db.cluster-x.us-east-1.rds.amazonaws.com",
    )
    _seed_env(
        tmp_db, name="DATABASE_URL",
        value="postgres://datatalks-prod-db.cluster-x.us-east-1.rds.amazonaws.com:5432/payments",
    )

    stats = infer_value_matches(tmp_db)
    assert stats.edges_inferred == 1

    edges = _edges_from_file(tmp_db, "/repo/app/.env")
    assert len(edges) == 1
    e = edges[0]
    assert e["dst_kind"] == "resource"
    assert e["dst_id"] == rds_uid
    assert e["rel_type"] == "DEPENDS_ON"
    props = json.loads(e["properties"])
    assert props["inferred_from"] == "value_match"
    assert props["via_entity"] == "DATABASE_URL"
    assert props["match_field"] == "endpoint"
    assert props["match_score"] == 1.0


# ---------------------------------------------------------------------------
# 2. Substring match (hostname only, no scheme/port)
# ---------------------------------------------------------------------------

def test_bare_hostname_substring_match(tmp_db):
    _seed_file_and_project(tmp_db)
    _seed_rds(
        tmp_db,
        name="payments-prod",
        endpoint="payments-prod.cluster-abc.us-east-1.rds.amazonaws.com",
    )
    _seed_env(
        tmp_db, name="DB_HOST",
        value="payments-prod.cluster-abc.us-east-1.rds.amazonaws.com",
    )
    stats = infer_value_matches(tmp_db)
    assert stats.edges_inferred == 1


# ---------------------------------------------------------------------------
# 3. ARN match
# ---------------------------------------------------------------------------

def test_arn_match(tmp_db):
    _seed_file_and_project(tmp_db, file_path="/repo/iam.yaml")
    upsert_file(tmp_db, FileRow(
        path="aws://9/us-east-1/sqs", project_path="",
        name="aws:sqs", extension="",
        size_bytes=0, content_hash="aws:9:us-east-1:sqs",
        modified_at="2026-05-16T00:00:00Z",
    ))
    arn = "arn:aws:sqs:us-east-1:999:payments-queue"
    uid = resource_uid("aws:sqs:Queue", "payments-queue", "us-east-1", "aws://9/us-east-1/sqs")
    upsert_resource(tmp_db, ResourceRow(
        uid=uid, file_path="aws://9/us-east-1/sqs",
        kind="aws:sqs:Queue", name="payments-queue",
        namespace="us-east-1", environment="production", risk_tier="red",
        properties={"arn": arn, "discovered_from": "aws-cli"},
    ))
    _seed_env(
        tmp_db, name="QUEUE_ARN",
        value=arn,
        file_path="/repo/iam.yaml",
    )
    stats = infer_value_matches(tmp_db)
    assert stats.edges_inferred == 1


# ---------------------------------------------------------------------------
# 4. Unrelated hostname → no edge
# ---------------------------------------------------------------------------

def test_unrelated_hostname_does_not_match(tmp_db):
    _seed_file_and_project(tmp_db)
    _seed_rds(
        tmp_db, name="prod-db",
        endpoint="prod-db.cluster-1.us-east-1.rds.amazonaws.com",
    )
    _seed_env(
        tmp_db, name="EXTERNAL_API",
        value="https://api.stripe.com/v1/charges",
    )
    stats = infer_value_matches(tmp_db)
    assert stats.edges_inferred == 0


# ---------------------------------------------------------------------------
# 5. Too short → no edge
# ---------------------------------------------------------------------------

def test_short_value_skipped(tmp_db):
    _seed_file_and_project(tmp_db)
    _seed_rds(tmp_db, name="db", endpoint="db.x.y.com")  # short canonical
    _seed_env(tmp_db, name="X", value="db")
    stats = infer_value_matches(tmp_db)
    # Short value either skipped or doesn't reach threshold.
    assert stats.edges_inferred == 0


# ---------------------------------------------------------------------------
# 6. Two similar resources — match to the more specific one
# ---------------------------------------------------------------------------

def test_two_similar_resources_match_correctly(tmp_db):
    _seed_file_and_project(tmp_db)
    _seed_rds(
        tmp_db, name="prod-db",
        endpoint="prod-db.cluster-1.us-east-1.rds.amazonaws.com",
        file_path="aws://1/us-east-1/rds-prod",
    )
    _seed_rds(
        tmp_db, name="prod-db-readonly",
        endpoint="prod-db-readonly.cluster-1.us-east-1.rds.amazonaws.com",
        file_path="aws://1/us-east-1/rds-readonly",
    )
    _seed_env(
        tmp_db, name="DB_HOST",
        value="prod-db-readonly.cluster-1.us-east-1.rds.amazonaws.com",
    )
    stats = infer_value_matches(tmp_db)
    assert stats.edges_inferred == 1
    edges = _edges_from_file(tmp_db, "/repo/app/.env")
    # The match should be to the readonly variant — exact match wins over substring.
    assert any("prod-db-readonly" in str(json.loads(e["properties"])["match_value"])
               for e in edges)


# ---------------------------------------------------------------------------
# 7. Idempotency
# ---------------------------------------------------------------------------

def test_idempotent_re_run(tmp_db):
    _seed_file_and_project(tmp_db)
    _seed_rds(
        tmp_db, name="cache-prod",
        endpoint="cache-prod.cluster-1.us-east-1.cache.amazonaws.com",
    )
    _seed_env(
        tmp_db, name="REDIS_URL",
        value="redis://cache-prod.cluster-1.us-east-1.cache.amazonaws.com:6379",
    )
    infer_value_matches(tmp_db)
    infer_value_matches(tmp_db)
    infer_value_matches(tmp_db)
    edges = _edges_from_file(tmp_db, "/repo/app/.env")
    # Three runs, one edge — UNIQUE constraint on (src, dst, rel_type) is honored.
    assert len(edges) == 1


# ---------------------------------------------------------------------------
# 8. End-to-end: blast_radius surfaces the inferred dependency
# ---------------------------------------------------------------------------

def test_blast_radius_sees_inferred_dependents(tmp_db):
    """Click-ops scenario: an RDS exists only because someone discovered it via
    aws-cli; an .env file references its endpoint. blast_radius should now
    surface the .env as a dependent of the RDS."""
    from engram.safety.blast_radius import assess

    _seed_file_and_project(tmp_db, file_path="/repo/payments/.env")
    rds_uid = _seed_rds(
        tmp_db,
        name="payments-prod-db",
        endpoint="payments-prod-db.cluster-x.us-east-1.rds.amazonaws.com",
    )
    _seed_env(
        tmp_db, name="DATABASE_URL",
        value="postgres://payments-prod-db.cluster-x.us-east-1.rds.amazonaws.com:5432/p",
        file_path="/repo/payments/.env",
    )
    infer_value_matches(tmp_db)

    result = assess(tmp_db, "terraform destroy", "payments-prod-db")
    assert result.action == "block"
    assert result.environment == "production"
    # The inferred edge means the .env file is a dependent of the RDS.
    assert len(result.dependents) >= 1
    assert any(d["kind"] == "file" and "/repo/payments/.env" in d["id"]
               for d in result.dependents)
