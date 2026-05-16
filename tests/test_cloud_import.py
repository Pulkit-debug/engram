"""Tests for the cloud + cluster importers.

We don't shell out for real — we monkey-patch `subprocess.run` to return
canned JSON that mirrors actual AWS CLI / kubectl output. This keeps the
test suite cloud-free and CI-fast.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Mock harness
# ---------------------------------------------------------------------------

def _mk_completed(stdout: str, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def _patch_aws(monkeypatch, responses: dict[tuple[str, ...], dict]):
    """Patch subprocess.run so AWS CLI calls return canned JSON.

    responses maps a tuple of CLI args (after 'aws') to a JSON-serializable
    response dict. Unmatched calls return {}.
    """
    def fake_run(argv, capture_output=False, text=False, timeout=None):
        # argv looks like: ['aws', 'rds', 'describe-db-instances', '--output', 'json', '--region', '...']
        # we key on the first few args (the subcommand path), ignoring flags.
        cmd_args: list[str] = []
        i = 1  # skip 'aws'
        while i < len(argv):
            a = argv[i]
            if a.startswith("--"):
                i += 2  # skip flag + value
                continue
            cmd_args.append(a)
            i += 1
        key = tuple(cmd_args)
        # Try exact match, then partial-prefix.
        if key in responses:
            return _mk_completed(json.dumps(responses[key]))
        for k, v in responses.items():
            if key[:len(k)] == k:
                return _mk_completed(json.dumps(v))
        return _mk_completed("{}")
    monkeypatch.setattr("engram.cloud.aws_import.subprocess.run", fake_run)
    monkeypatch.setattr("engram.cloud.aws_import.shutil.which", lambda x: "/usr/local/bin/aws")


def _patch_kubectl(monkeypatch, responses: dict[str, dict]):
    """Patch subprocess.run so kubectl calls return canned JSON.

    responses maps a kind ("Deployment", "Pod", ...) to the canned list response.
    Plus a special key "current-context" for `kubectl config current-context`.
    """
    def fake_run(argv, capture_output=False, text=False, timeout=None):
        if "config" in argv and "current-context" in argv:
            ctx = responses.get("current-context", "default")
            return _mk_completed(ctx + "\n")
        if "get" in argv:
            i = argv.index("get")
            if i + 1 < len(argv):
                kind = argv[i + 1]
                payload = responses.get(kind, {"items": []})
                return _mk_completed(json.dumps(payload))
        return _mk_completed("{}")
    monkeypatch.setattr("engram.cloud.kubectl_import.subprocess.run", fake_run)
    monkeypatch.setattr("engram.cloud.kubectl_import.shutil.which", lambda x: "/usr/local/bin/kubectl")


# ---------------------------------------------------------------------------
# AWS importer tests
# ---------------------------------------------------------------------------

def test_aws_import_rds_creates_resource_with_prod_tier(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws

    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "123456789012"},
        ("rds", "describe-db-instances"): {
            "DBInstances": [{
                "DBInstanceIdentifier": "datatalks-prod-db",
                "DBInstanceArn": "arn:aws:rds:us-east-1:123:db/datatalks-prod-db",
                "Engine": "postgres",
                "EngineVersion": "15.4",
                "DBInstanceClass": "db.r5.large",
                "DBInstanceStatus": "available",
                "MultiAZ": True,
                "Endpoint": {"Address": "datatalks-prod-db.xxx.rds.amazonaws.com"},
                "TagList": [
                    {"Key": "Environment", "Value": "production"},
                    {"Key": "Owner", "Value": "platform"},
                ],
            }],
        },
    })
    stats = import_aws(tmp_db, kinds=["rds"], region="us-east-1")
    assert stats.inserted == 1
    assert stats.per_kind["rds"] == 1

    row = tmp_db.execute(
        "SELECT kind, name, environment, risk_tier FROM resource"
    ).fetchone()
    assert row["kind"] == "aws:rds:DBInstance"
    assert row["name"] == "datatalks-prod-db"
    assert row["environment"] == "production"
    assert row["risk_tier"] == "red"


def test_aws_import_blast_radius_finds_clickops_resource(tmp_db, monkeypatch):
    """End-to-end: import a clicked-into-existence RDS and then assess against it."""
    from engram.cloud.aws_import import import_aws
    from engram.safety.blast_radius import assess

    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "999"},
        ("rds", "describe-db-instances"): {
            "DBInstances": [{
                "DBInstanceIdentifier": "users-prod",
                "DBInstanceArn": "arn:aws:rds:us-east-1:999:db/users-prod",
                "Engine": "mysql",
                "DBInstanceClass": "db.m5.large",
                "TagList": [{"Key": "Environment", "Value": "prod"}],
            }],
        },
    })
    import_aws(tmp_db, kinds=["rds"], region="us-east-1")

    # Now assess — this is the CLICK-OPS scenario the project was missing.
    result = assess(tmp_db, "terraform destroy", "users-prod")
    assert result.action == "block", "Click-ops prod resource should still trigger BLOCK"
    assert result.environment == "production"
    assert result.risk_tier == "red"


def test_aws_import_ec2_uses_name_tag(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws

    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("ec2", "describe-instances"): {
            "Reservations": [{
                "Instances": [{
                    "InstanceId": "i-abc123",
                    "InstanceType": "t3.medium",
                    "State": {"Name": "running"},
                    "PrivateIpAddress": "10.0.0.5",
                    "VpcId": "vpc-1",
                    "Tags": [
                        {"Key": "Name", "Value": "bastion-staging"},
                        {"Key": "env", "Value": "staging"},
                    ],
                }],
            }],
        },
    })
    stats = import_aws(tmp_db, kinds=["ec2"], region="us-east-1")
    assert stats.inserted == 1
    row = tmp_db.execute("SELECT name, environment, risk_tier FROM resource").fetchone()
    assert row["name"] == "bastion-staging"
    assert row["environment"] == "staging"
    assert row["risk_tier"] == "orange"


def test_aws_import_handles_cli_not_found(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    monkeypatch.setattr("engram.cloud.aws_import.shutil.which", lambda x: None)
    stats = import_aws(tmp_db, kinds=["rds"])
    assert stats.inserted == 0
    assert any("AWS CLI not found" in r[1] for r in stats.errors)


def test_aws_import_handles_unauthenticated(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    # Mock: aws exists, but get-caller-identity fails (no creds).
    def fake_run(argv, capture_output=False, text=False, timeout=None):
        if "get-caller-identity" in argv:
            return _mk_completed("Unable to locate credentials", returncode=255)
        return _mk_completed("{}")
    monkeypatch.setattr("engram.cloud.aws_import.subprocess.run", fake_run)
    monkeypatch.setattr("engram.cloud.aws_import.shutil.which", lambda x: "/usr/local/bin/aws")

    stats = import_aws(tmp_db, kinds=["rds"])
    assert stats.inserted == 0
    assert any("get-caller-identity" in r[1] or "configure" in r[1] for r in stats.errors)


def test_aws_import_s3_buckets(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("s3api", "list-buckets"): {
            "Buckets": [
                {"Name": "myco-prod-uploads", "CreationDate": "2024-01-01"},
                {"Name": "myco-dev-test-assets", "CreationDate": "2024-01-02"},
            ],
        },
    })
    import_aws(tmp_db, kinds=["s3"])
    rows = tmp_db.execute(
        "SELECT name, environment FROM resource ORDER BY name"
    ).fetchall()
    assert len(rows) == 2
    by_name = {r["name"]: r["environment"] for r in rows}
    assert by_name["myco-prod-uploads"] == "production"
    assert by_name["myco-dev-test-assets"] == "dev"


# ---------------------------------------------------------------------------
# kubectl importer tests
# ---------------------------------------------------------------------------

def test_kubectl_import_deployment(tmp_db, monkeypatch):
    from engram.cloud.kubectl_import import import_cluster

    _patch_kubectl(monkeypatch, {
        "current-context": "production-cluster",
        "Deployment": {
            "items": [{
                "metadata": {
                    "name": "payments",
                    "namespace": "prod",
                    "labels": {"app": "payments", "environment": "production"},
                },
                "spec": {
                    "replicas": 3,
                    "template": {"spec": {"containers": [
                        {"name": "payments", "image": "myco/payments:1.2.3"},
                    ]}},
                },
            }],
        },
    })
    stats = import_cluster(tmp_db, kinds=["Deployment"])
    assert stats.inserted == 1

    row = tmp_db.execute(
        "SELECT kind, name, namespace, environment, risk_tier FROM resource"
    ).fetchone()
    assert row["kind"] == "k8s:Deployment"
    assert row["name"] == "payments"
    assert row["namespace"] == "prod"
    assert row["environment"] == "production"
    assert row["risk_tier"] == "red"


def test_kubectl_import_picks_up_image_tech_edges(tmp_db, monkeypatch):
    from engram.cloud.kubectl_import import import_cluster

    _patch_kubectl(monkeypatch, {
        "current-context": "prod",
        "Deployment": {
            "items": [{
                "metadata": {"name": "api", "namespace": "default"},
                "spec": {"template": {"spec": {"containers": [
                    {"name": "api", "image": "ghcr.io/myco/api:v2"},
                    {"name": "redis", "image": "redis:7"},
                ]}}},
            }],
        },
    })
    import_cluster(tmp_db, kinds=["Deployment"])
    techs = {r[0] for r in tmp_db.execute("SELECT name FROM technology")}
    assert "redis" in techs
    assert "api" in techs
    # USES edge from the deployment to a technology.
    uses_count = tmp_db.execute(
        "SELECT count(*) FROM edge WHERE rel_type='USES' AND dst_kind='technology'"
    ).fetchone()[0]
    assert uses_count >= 2


def test_kubectl_import_handles_kubectl_missing(tmp_db, monkeypatch):
    from engram.cloud.kubectl_import import import_cluster
    monkeypatch.setattr("engram.cloud.kubectl_import.shutil.which", lambda x: None)
    stats = import_cluster(tmp_db, kinds=["Deployment"])
    assert stats.inserted == 0
    assert any("kubectl not found" in r[1] for r in stats.errors)


def test_kubectl_import_drift_with_yaml(tmp_db, tmp_path, monkeypatch):
    """A K8s manifest in YAML + the same resource discovered live → both
    rows in the graph, with the live one tagged `discovered_from`."""
    from engram.crawler import index_paths
    from engram.cloud.kubectl_import import import_cluster
    from engram.config import Config

    # Step 1: YAML manifest.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "payments.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: payments\n"
        "  namespace: prod\nspec:\n  replicas: 3\n",
        encoding="utf-8",
    )
    cfg = Config(data_dir=tmp_path / "data", log_dir=tmp_path / "logs",
                 watch_paths=[repo], embeddings_enabled=False)
    cfg.ensure_dirs()
    index_paths(tmp_db, cfg)

    # Step 2: same name discovered live.
    _patch_kubectl(monkeypatch, {
        "current-context": "prod-cluster",
        "Deployment": {
            "items": [{
                "metadata": {"name": "payments", "namespace": "prod",
                             "labels": {"environment": "production"}},
                "spec": {"replicas": 5},  # NB: drift — YAML says 3, live says 5
            }],
        },
    })
    import_cluster(tmp_db, kinds=["Deployment"])

    rows = tmp_db.execute(
        "SELECT name, namespace, properties FROM resource "
        "WHERE name = 'payments' ORDER BY file_path"
    ).fetchall()
    assert len(rows) == 2  # one from YAML, one from live cluster
    # The live one carries the discovered_from marker.
    live_count = sum(
        1 for r in rows
        if '"discovered_from": "kubectl"' in r["properties"]
    )
    assert live_count == 1
