"""Tests for the v0.3 AWS importers: VPC, Subnet, SecurityGroup, IAM,
SecretsManager, Route53. Same mock pattern as test_cloud_import.py."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace


def _mk(stdout: str, code: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout=stdout, stderr="")


def _patch_aws(monkeypatch, responses: dict[tuple[str, ...], dict]):
    """Same patcher as in test_cloud_import.py; duplicated here so tests stay
    self-contained and the mock harness is visible."""

    def fake_run(argv, capture_output=False, text=False, timeout=None):
        cmd_args: list[str] = []
        i = 1
        while i < len(argv):
            a = argv[i]
            if a.startswith("--"):
                i += 2
                continue
            cmd_args.append(a)
            i += 1
        key = tuple(cmd_args)
        if key in responses:
            return _mk(json.dumps(responses[key]))
        for k, v in responses.items():
            if key[:len(k)] == k:
                return _mk(json.dumps(v))
        return _mk("{}")

    monkeypatch.setattr("engram.cloud.aws_import.subprocess.run", fake_run)
    monkeypatch.setattr("engram.cloud.aws_import.shutil.which", lambda x: "/usr/local/bin/aws")


# ---------------------------------------------------------------------------
# VPC
# ---------------------------------------------------------------------------

def test_import_vpc(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("ec2", "describe-vpcs"): {
            "Vpcs": [
                {
                    "VpcId": "vpc-prod123",
                    "CidrBlock": "10.0.0.0/16",
                    "IsDefault": False,
                    "State": "available",
                    "Tags": [
                        {"Key": "Name", "Value": "prod-vpc"},
                        {"Key": "Environment", "Value": "production"},
                    ],
                },
                {
                    "VpcId": "vpc-default",
                    "CidrBlock": "172.31.0.0/16",
                    "IsDefault": True,
                    "State": "available",
                    "Tags": [],
                },
            ],
        },
    })
    stats = import_aws(tmp_db, kinds=["vpc"], region="us-east-1")
    assert stats.inserted == 2
    rows = tmp_db.execute(
        "SELECT name, environment, risk_tier FROM resource ORDER BY name"
    ).fetchall()
    by_name = {r["name"]: (r["environment"], r["risk_tier"]) for r in rows}
    assert by_name["prod-vpc"] == ("production", "red")
    # Default VPC has no name tag and no env hint → falls back to VpcId 'vpc-default'.
    assert "vpc-default" in by_name


# ---------------------------------------------------------------------------
# Subnet
# ---------------------------------------------------------------------------

def test_import_subnet(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("ec2", "describe-subnets"): {
            "Subnets": [{
                "SubnetId": "subnet-prod1",
                "VpcId": "vpc-prod",
                "CidrBlock": "10.0.1.0/24",
                "AvailabilityZone": "us-east-1a",
                "Tags": [{"Key": "Name", "Value": "prod-private-1a"}],
            }],
        },
    })
    stats = import_aws(tmp_db, kinds=["subnet"], region="us-east-1")
    assert stats.inserted == 1
    row = tmp_db.execute(
        "SELECT name, kind, environment FROM resource"
    ).fetchone()
    assert row["name"] == "prod-private-1a"
    assert row["kind"] == "aws:ec2:Subnet"
    assert row["environment"] == "production"


# ---------------------------------------------------------------------------
# Security Group
# ---------------------------------------------------------------------------

def test_import_security_group_with_ingress_egress(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("ec2", "describe-security-groups"): {
            "SecurityGroups": [{
                "GroupId": "sg-abc",
                "GroupName": "payments-prod-sg",
                "VpcId": "vpc-prod",
                "Description": "Allow ALB in",
                "Tags": [{"Key": "Environment", "Value": "production"}],
                "IpPermissions": [{"IpProtocol": "tcp", "FromPort": 443}],
                "IpPermissionsEgress": [
                    {"IpProtocol": "-1"},
                    {"IpProtocol": "tcp", "FromPort": 443},
                ],
            }],
        },
    })
    import_aws(tmp_db, kinds=["sg"], region="us-east-1")
    row = tmp_db.execute("SELECT name, properties FROM resource").fetchone()
    assert row["name"] == "payments-prod-sg"
    props = json.loads(row["properties"])
    assert props["ingress_rule_count"] == 1
    assert props["egress_rule_count"] == 2


# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------

def test_import_iam_roles(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "999"},
        ("iam", "list-roles"): {
            "Roles": [
                {
                    "RoleName": "prod-payments-task-role",
                    "RoleId": "AROA1",
                    "Arn": "arn:aws:iam::999:role/prod-payments-task-role",
                    "Path": "/",
                    "CreateDate": "2024-09-01",
                },
                {
                    "RoleName": "developer-readonly",
                    "RoleId": "AROA2",
                    "Arn": "arn:aws:iam::999:role/developer-readonly",
                    "Path": "/",
                    "CreateDate": "2023-01-01",
                },
            ],
        },
    })
    stats = import_aws(tmp_db, kinds=["iam"])
    assert stats.inserted == 2
    rows = tmp_db.execute(
        "SELECT name, namespace, environment, risk_tier FROM resource ORDER BY name"
    ).fetchall()
    by_name = {r["name"]: r for r in rows}
    # IAM is global; namespace should be 'global', not a region.
    assert by_name["prod-payments-task-role"]["namespace"] == "global"
    assert by_name["prod-payments-task-role"]["environment"] == "production"
    assert by_name["prod-payments-task-role"]["risk_tier"] == "red"
    assert by_name["developer-readonly"]["environment"] == "dev"


# ---------------------------------------------------------------------------
# SecretsManager + SSM Parameter Store
# ---------------------------------------------------------------------------

def test_import_secrets_both_secretsmanager_and_ssm(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("secretsmanager", "list-secrets"): {
            "SecretList": [{
                "Name": "prod/db/payments",
                "ARN": "arn:aws:secretsmanager:us-east-1:1:secret:prod/db/payments-abc",
                "Description": "Payments DB credentials",
                "LastChangedDate": "2025-12-01",
                "Tags": [{"Key": "Environment", "Value": "production"}],
            }],
        },
        ("ssm", "describe-parameters"): {
            "Parameters": [{
                "Name": "/prod/api/stripe-public-key",
                "Type": "String",
                "Tier": "Standard",
                "LastModifiedDate": "2025-11-01",
                "Version": 3,
            }],
        },
    })
    stats = import_aws(tmp_db, kinds=["secrets"], region="us-east-1")
    assert stats.inserted == 2
    rows = tmp_db.execute(
        "SELECT name, kind, environment FROM resource ORDER BY name"
    ).fetchall()
    by_name = {r["name"]: r for r in rows}
    assert by_name["prod/db/payments"]["kind"] == "aws:secretsmanager:Secret"
    assert by_name["prod/db/payments"]["environment"] == "production"
    assert by_name["/prod/api/stripe-public-key"]["kind"] == "aws:ssm:Parameter"
    assert by_name["/prod/api/stripe-public-key"]["environment"] == "production"


# ---------------------------------------------------------------------------
# Route53
# ---------------------------------------------------------------------------

def test_import_route53_zones_and_records(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("route53", "list-hosted-zones"): {
            "HostedZones": [{
                "Id": "/hostedzone/Z1ABC",
                "Name": "prod.myco.com.",
                "Config": {"PrivateZone": False},
                "ResourceRecordSetCount": 5,
            }],
        },
        ("route53", "list-resource-record-sets"): {
            "ResourceRecordSets": [
                {
                    "Name": "api.prod.myco.com.",
                    "Type": "A",
                    "TTL": 60,
                    "ResourceRecords": [{"Value": "1.2.3.4"}],
                },
                {
                    "Name": "db.prod.myco.com.",
                    "Type": "CNAME",
                    "TTL": 300,
                    "ResourceRecords": [
                        {"Value": "payments-prod.cluster-x.us-east-1.rds.amazonaws.com"},
                    ],
                },
                # Should be ignored — TXT record.
                {
                    "Name": "_dmarc.prod.myco.com.",
                    "Type": "TXT",
                    "TTL": 300,
                    "ResourceRecords": [{"Value": "\"v=DMARC1; p=none\""}],
                },
            ],
        },
    })
    stats = import_aws(tmp_db, kinds=["route53"])
    # 1 zone + 2 records (A + CNAME) = 3. TXT skipped.
    assert stats.inserted == 3
    rows = tmp_db.execute(
        "SELECT kind, name FROM resource ORDER BY kind, name"
    ).fetchall()
    kinds = [r["kind"] for r in rows]
    assert "aws:route53:HostedZone" in kinds
    assert "aws:route53:RecordSet:A" in kinds
    assert "aws:route53:RecordSet:CNAME" in kinds


# ---------------------------------------------------------------------------
# Route53 + value_match: env var matches a CNAME target
# ---------------------------------------------------------------------------

def test_route53_record_enables_env_var_match(tmp_db, monkeypatch):
    """A CNAME pointing at an RDS endpoint should be matchable by an env var
    that references the friendly DNS name."""
    from engram.cloud.aws_import import import_aws
    from engram.inference.value_match import infer_value_matches
    from engram.graph import (
        EntityRow, FileRow, ProjectRow,
        entity_uid, upsert_entity, upsert_file, upsert_project,
    )

    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("route53", "list-hosted-zones"): {
            "HostedZones": [{
                "Id": "/hostedzone/Z1",
                "Name": "prod.myco.com.",
                "Config": {"PrivateZone": False},
                "ResourceRecordSetCount": 1,
            }],
        },
        ("route53", "list-resource-record-sets"): {
            "ResourceRecordSets": [{
                "Name": "db.prod.myco.com.",
                "Type": "CNAME",
                "TTL": 300,
                "ResourceRecords": [
                    {"Value": "payments-prod.cluster.us-east-1.rds.amazonaws.com"},
                ],
            }],
        },
    })
    import_aws(tmp_db, kinds=["route53"])

    # Seed an env var that uses the friendly DNS name.
    upsert_project(tmp_db, ProjectRow(path="/repo", name="app"))
    upsert_file(tmp_db, FileRow(
        path="/repo/.env", project_path="/repo",
        name=".env", extension=".env",
        size_bytes=10, content_hash="h",
        modified_at="2026-05-16T00:00:00Z",
    ))
    upsert_entity(tmp_db, EntityRow(
        uid=entity_uid("DATABASE_HOST", "env_var", "/repo/.env"),
        file_path="/repo/.env",
        name="DATABASE_HOST",
        entity_type="env_var",
        value="db.prod.myco.com",
    ))

    stats = infer_value_matches(tmp_db)
    assert stats.edges_inferred >= 1
    edges = tmp_db.execute(
        "SELECT properties FROM edge WHERE src_kind='file' AND src_id='/repo/.env' "
        "AND rel_type='DEPENDS_ON'"
    ).fetchall()
    assert len(edges) >= 1
    props = json.loads(edges[0]["properties"])
    assert props["inferred_from"] == "value_match"
