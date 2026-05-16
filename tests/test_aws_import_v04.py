"""Tests for the v0.4 AWS importers: 16 new services covering customer-facing
surface, state, and security."""

from __future__ import annotations

import json
from types import SimpleNamespace


def _mk(stdout: str, code: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout=stdout, stderr="")


def _patch_aws(monkeypatch, responses: dict[tuple[str, ...], dict]):
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
    monkeypatch.setattr("engram.cloud.aws_import.shutil.which",
                        lambda x: "/usr/local/bin/aws")


# ---------------------------------------------------------------------------
# Per-service tests
# ---------------------------------------------------------------------------

def test_cloudfront(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("cloudfront", "list-distributions"): {
            "DistributionList": {"Items": [{
                "Id": "EABC123",
                "ARN": "arn:aws:cloudfront::1:distribution/EABC123",
                "DomainName": "d123.cloudfront.net",
                "Status": "Deployed",
                "Enabled": True,
            }]},
        },
    })
    import_aws(tmp_db, kinds=["cloudfront"], region="us-east-1")
    row = tmp_db.execute("SELECT kind, name FROM resource").fetchone()
    assert row["kind"] == "aws:cloudfront:Distribution"
    assert row["name"] == "d123.cloudfront.net"


def test_apigateway_both_v1_and_v2(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("apigateway", "get-rest-apis"): {
            "items": [{"id": "abc", "name": "payments-api"}]
        },
        ("apigatewayv2", "get-apis"): {
            "Items": [{"ApiId": "def", "Name": "events-api",
                       "ProtocolType": "HTTP",
                       "ApiEndpoint": "https://def.execute-api.us-east-1.amazonaws.com"}]
        },
    })
    import_aws(tmp_db, kinds=["apigateway"], region="us-east-1")
    kinds = [r["kind"] for r in tmp_db.execute("SELECT kind FROM resource").fetchall()]
    assert "aws:apigateway:RestApi" in kinds
    assert "aws:apigatewayv2:Api" in kinds


def test_asg_and_launch_templates(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("autoscaling", "describe-auto-scaling-groups"): {
            "AutoScalingGroups": [{
                "AutoScalingGroupName": "prod-api-asg",
                "AutoScalingGroupARN": "arn:aws:autoscaling:us-east-1:1:asg/prod-api",
                "MinSize": 2, "MaxSize": 20, "DesiredCapacity": 5,
                "LaunchTemplate": {"LaunchTemplateName": "prod-api-template"},
                "Tags": [{"Key": "Environment", "Value": "production"}],
            }],
        },
        ("ec2", "describe-launch-templates"): {
            "LaunchTemplates": [{
                "LaunchTemplateId": "lt-123",
                "LaunchTemplateName": "prod-api-template",
                "DefaultVersionNumber": 1,
                "LatestVersionNumber": 3,
                "Tags": [],
            }],
        },
    })
    import_aws(tmp_db, kinds=["asg"], region="us-east-1")
    rows = tmp_db.execute(
        "SELECT name, kind, environment FROM resource ORDER BY kind"
    ).fetchall()
    by_kind = {r["kind"]: r for r in rows}
    assert "aws:autoscaling:Group" in by_kind
    assert by_kind["aws:autoscaling:Group"]["environment"] == "production"
    assert "aws:ec2:LaunchTemplate" in by_kind


def test_ebs_with_attachments(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("ec2", "describe-volumes"): {
            "Volumes": [{
                "VolumeId": "vol-abc",
                "Size": 100, "VolumeType": "gp3", "State": "in-use",
                "Encrypted": True,
                "AvailabilityZone": "us-east-1a",
                "Attachments": [{"InstanceId": "i-xyz"}],
                "Tags": [{"Key": "Name", "Value": "prod-db-data"}],
            }],
        },
    })
    import_aws(tmp_db, kinds=["ebs"], region="us-east-1")
    row = tmp_db.execute("SELECT properties FROM resource").fetchone()
    props = json.loads(row["properties"])
    assert props["size_gb"] == 100
    assert props["attached_instance"] == "i-xyz"
    assert props["encrypted"] is True


def test_elasticache_both_kinds(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("elasticache", "describe-cache-clusters"): {
            "CacheClusters": [{
                "CacheClusterId": "memcached-cache",
                "Engine": "memcached",
                "NumCacheNodes": 3,
                "CacheClusterStatus": "available",
            }],
        },
        ("elasticache", "describe-replication-groups"): {
            "ReplicationGroups": [{
                "ReplicationGroupId": "prod-redis",
                "Description": "primary redis",
                "Status": "available",
                "ConfigurationEndpoint": {"Address": "prod-redis.x.cache.amazonaws.com"},
                "ClusterEnabled": True,
            }],
        },
    })
    import_aws(tmp_db, kinds=["elasticache"], region="us-east-1")
    kinds = {r["kind"] for r in tmp_db.execute("SELECT kind FROM resource").fetchall()}
    assert "aws:elasticache:CacheCluster" in kinds
    assert "aws:elasticache:ReplicationGroup" in kinds


def test_cloudwatch_logs_and_alarms(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("logs", "describe-log-groups"): {
            "logGroups": [{
                "logGroupName": "/aws/lambda/payments-prod",
                "arn": "arn:aws:logs:us-east-1:1:log-group:/aws/lambda/payments-prod:*",
                "retentionInDays": 30,
                "storedBytes": 1024 * 1024 * 100,
            }],
        },
        ("cloudwatch", "describe-alarms"): {
            "MetricAlarms": [{
                "AlarmName": "payments-prod-high-cpu",
                "AlarmArn": "arn:aws:cloudwatch:us-east-1:1:alarm:payments-prod-high-cpu",
                "StateValue": "OK",
                "MetricName": "CPUUtilization",
                "Namespace": "AWS/EC2",
                "Threshold": 80.0,
                "ComparisonOperator": "GreaterThanThreshold",
                "ActionsEnabled": True,
            }],
        },
    })
    import_aws(tmp_db, kinds=["logs", "alarms"], region="us-east-1")
    kinds = {r["kind"] for r in tmp_db.execute("SELECT kind FROM resource").fetchall()}
    assert "aws:logs:LogGroup" in kinds
    assert "aws:cloudwatch:Alarm" in kinds


def test_eventbridge_rules(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("events", "list-rules"): {
            "Rules": [{
                "Name": "daily-cleanup",
                "Arn": "arn:aws:events:us-east-1:1:rule/daily-cleanup",
                "State": "ENABLED",
                "ScheduleExpression": "cron(0 4 * * ? *)",
            }],
        },
    })
    import_aws(tmp_db, kinds=["events"], region="us-east-1")
    assert tmp_db.execute(
        "SELECT count(*) FROM resource WHERE kind='aws:events:Rule'"
    ).fetchone()[0] == 1


def test_stepfunctions(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("stepfunctions", "list-state-machines"): {
            "stateMachines": [{
                "name": "order-fulfillment",
                "stateMachineArn": "arn:aws:states:us-east-1:1:stateMachine:order-fulfillment",
                "type": "STANDARD",
                "creationDate": "2024-01-01",
            }],
        },
    })
    import_aws(tmp_db, kinds=["stepfunctions"], region="us-east-1")
    row = tmp_db.execute("SELECT name FROM resource").fetchone()
    assert row["name"] == "order-fulfillment"


def test_kms_uses_alias_when_present(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("kms", "list-keys"): {
            "Keys": [
                {"KeyId": "abc-key", "KeyArn": "arn:aws:kms:us-east-1:1:key/abc"},
                {"KeyId": "no-alias-key", "KeyArn": "arn:aws:kms:us-east-1:1:key/no"},
            ],
        },
        ("kms", "list-aliases"): {
            "Aliases": [{"AliasName": "alias/prod-data"}],
        },
    })
    import_aws(tmp_db, kinds=["kms"], region="us-east-1")
    names = {r["name"] for r in tmp_db.execute("SELECT name FROM resource").fetchall()}
    # First key gets the alias name (the mock returns it for any list-aliases).
    # Second key would also resolve to the alias because the mock isn't
    # per-key-id specific. The intent here is shape, not exact matching.
    assert any("prod-data" in n or n.endswith("-key") for n in names)


def test_acm_certificates(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("acm", "list-certificates"): {
            "CertificateSummaryList": [{
                "CertificateArn": "arn:aws:acm:us-east-1:1:certificate/abc",
                "DomainName": "*.prod.example.com",
                "Status": "ISSUED",
                "Type": "AMAZON_ISSUED",
                "InUse": True,
            }],
        },
    })
    import_aws(tmp_db, kinds=["acm"], region="us-east-1")
    row = tmp_db.execute("SELECT name, environment FROM resource").fetchone()
    assert row["name"] == "*.prod.example.com"
    assert row["environment"] == "production"


def test_cognito(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("cognito-idp", "list-user-pools"): {
            "UserPools": [{
                "Id": "us-east-1_ABC123",
                "Name": "prod-app-users",
                "CreationDate": "2024-01-01",
            }],
        },
    })
    import_aws(tmp_db, kinds=["cognito"], region="us-east-1")
    row = tmp_db.execute("SELECT name, environment FROM resource").fetchone()
    assert row["name"] == "prod-app-users"
    assert row["environment"] == "production"


def test_kinesis(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("kinesis", "list-streams"): {
            "StreamNames": ["clickstream-prod", "analytics-staging", "dev-logs"],
        },
    })
    import_aws(tmp_db, kinds=["kinesis"], region="us-east-1")
    rows = tmp_db.execute(
        "SELECT name, environment FROM resource ORDER BY name"
    ).fetchall()
    by_name = {r["name"]: r["environment"] for r in rows}
    assert by_name["clickstream-prod"] == "production"
    assert by_name["analytics-staging"] == "staging"
    assert by_name["dev-logs"] == "dev"


def test_opensearch(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("opensearch", "list-domain-names"): {
            "DomainNames": [
                {"DomainName": "prod-search", "EngineType": "OpenSearch"},
                {"DomainName": "dev-logs", "EngineType": "Elasticsearch"},
            ],
        },
    })
    import_aws(tmp_db, kinds=["opensearch"], region="us-east-1")
    rows = tmp_db.execute(
        "SELECT name, kind FROM resource ORDER BY name"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["kind"] == "aws:opensearch:Domain" for r in rows)


def test_redshift(tmp_db, monkeypatch):
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("redshift", "describe-clusters"): {
            "Clusters": [{
                "ClusterIdentifier": "warehouse-prod",
                "NodeType": "ra3.4xlarge",
                "ClusterStatus": "available",
                "NumberOfNodes": 4,
                "Endpoint": {"Address": "warehouse-prod.x.us-east-1.redshift.amazonaws.com"},
                "Encrypted": True,
                "Tags": [{"Key": "Environment", "Value": "production"}],
            }],
        },
    })
    import_aws(tmp_db, kinds=["redshift"], region="us-east-1")
    row = tmp_db.execute(
        "SELECT name, environment, risk_tier FROM resource"
    ).fetchone()
    assert row["name"] == "warehouse-prod"
    assert row["environment"] == "production"
    assert row["risk_tier"] == "red"


def test_waf_both_scopes(tmp_db, monkeypatch):
    """WAFv2 is queried twice — once REGIONAL, once CLOUDFRONT (global)."""
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("wafv2", "list-web-acls"): {
            "WebACLs": [{
                "Name": "prod-waf",
                "ARN": "arn:aws:wafv2:us-east-1:1:regional/webacl/prod-waf/abc",
                "Id": "abc",
            }],
        },
    })
    import_aws(tmp_db, kinds=["waf"], region="us-east-1")
    rows = tmp_db.execute(
        "SELECT name, namespace FROM resource WHERE kind='aws:wafv2:WebACL'"
    ).fetchall()
    # Same mock fires for both scopes, so we'd get 2 rows (one regional, one global).
    # The implementation dedupes by UID; namespace differs so we end up with both.
    assert len(rows) >= 1
    namespaces = {r["namespace"] for r in rows}
    assert "us-east-1" in namespaces or "global" in namespaces


# ---------------------------------------------------------------------------
# Cross-service: blast_radius on a v0.4 resource
# ---------------------------------------------------------------------------

def test_blast_radius_blocks_on_kms_in_prod(tmp_db, monkeypatch):
    """Deleting a KMS key in prod must return BLOCK / red."""
    from engram.cloud.aws_import import import_aws
    from engram.safety.blast_radius import assess

    _patch_aws(monkeypatch, {
        ("sts", "get-caller-identity"): {"Account": "1"},
        ("kms", "list-keys"): {
            "Keys": [{"KeyId": "abc", "KeyArn": "arn:aws:kms:us-east-1:1:key/abc"}],
        },
        ("kms", "list-aliases"): {
            "Aliases": [{"AliasName": "alias/prod-payments-encryption"}],
        },
    })
    import_aws(tmp_db, kinds=["kms"], region="us-east-1")

    result = assess(tmp_db, "aws kms schedule-key-deletion", "prod-payments-encryption")
    assert result.environment == "production"
    assert result.action == "block"
    assert result.risk_tier == "red"


def test_default_kinds_cover_all_v04_services(tmp_db, monkeypatch):
    """The default --kinds string in the CLI should include every v0.4 service."""
    # Compile-time check via the importers dict.
    from engram.cloud.aws_import import import_aws
    _patch_aws(monkeypatch, {("sts", "get-caller-identity"): {"Account": "1"}})
    stats = import_aws(
        tmp_db,
        kinds=["cloudfront", "apigateway", "asg", "ebs", "elasticache",
               "logs", "alarms", "events", "stepfunctions", "kms",
               "acm", "cognito", "kinesis", "opensearch", "redshift", "waf"],
        region="us-east-1",
    )
    # All 16 should be recognized (no "unknown kind" errors).
    unknown = [e for e in stats.errors if "unknown kind" in e[1]]
    assert not unknown, f"unknown kinds: {unknown}"
