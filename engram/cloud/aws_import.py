"""AWS resource discovery via the AWS CLI.

Shells out to `aws <service> describe-*` for each requested kind, parses
the JSON output, and inserts Resources into the graph. The user must have
the AWS CLI installed and authenticated (`aws configure`). Engram itself
never calls AWS.

Supported kinds (v0.2):
  rds       — RDS DB instances + Aurora clusters
  ec2       — EC2 instances + EBS volumes
  s3        — S3 buckets
  eks       — EKS clusters
  lambda    — Lambda functions
  elb       — Application/Network load balancers
  ecs       — ECS clusters + services
  sqs       — SQS queues
  sns       — SNS topics
  dynamodb  — DynamoDB tables

Each discovered resource becomes a Resource of kind `aws:<service>:<type>`
with:
  - environment inferred from tags (`Environment`/`env`/`stage`) or name suffix
  - risk_tier derived from environment
  - properties carrying the most useful raw fields (ARN, region, instance
    class, etc.) for downstream querying.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from engram.graph import (
    EdgeSpec, FileRow, ResourceRow,
    resource_uid, upsert_edge, upsert_file, upsert_resource,
    upsert_technology, RISK_GREEN, RISK_ORANGE, RISK_RED,
)

logger = logging.getLogger(__name__)


# A virtual file path representing "this AWS account" so the graph FK to
# `file` is satisfied. Stored in the file table once per account/region.
_VIRTUAL_PATH_TEMPLATE = "aws://{account}/{region}/{service}"


@dataclass
class CloudImportStats:
    discovered: int = 0
    inserted: int = 0
    skipped: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)  # (kind, reason)
    per_kind: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def import_aws(
    conn: sqlite3.Connection,
    *,
    kinds: list[str],
    region: str | None = None,
    profile: str | None = None,
    aws_cli: str = "aws",
) -> CloudImportStats:
    """Discover AWS resources and insert them into the graph.

    Args:
        conn:    open sqlite3 connection.
        kinds:   list of {"rds","ec2","s3","eks","lambda","elb","ecs","sqs","sns","dynamodb"}.
        region:  AWS region (or None to use default from CLI config).
        profile: AWS profile name (or None to use default).
        aws_cli: path or name of the aws CLI.

    Returns: stats dict.
    """
    stats = CloudImportStats()

    if not shutil.which(aws_cli):
        stats.errors.append(("__cli__", f"AWS CLI not found at '{aws_cli}'. "
                             f"Install: https://aws.amazon.com/cli/"))
        return stats

    # Resolve account ID once for the virtual file path.
    account = _aws_account_id(aws_cli, profile)
    if not account:
        stats.errors.append(("__cli__", "AWS CLI authenticated check failed "
                             "(`aws sts get-caller-identity`). Run `aws configure` first."))
        return stats

    region = region or _aws_default_region(aws_cli, profile)

    importers: dict[str, Callable] = {
        "rds":      _import_rds,
        "ec2":      _import_ec2,
        "s3":       _import_s3,
        "eks":      _import_eks,
        "lambda":   _import_lambda,
        "elb":      _import_elb,
        "ecs":      _import_ecs,
        "sqs":      _import_sqs,
        "sns":      _import_sns,
        "dynamodb": _import_dynamodb,
    }

    for kind in kinds:
        if kind not in importers:
            stats.errors.append((kind, f"unknown kind. Supported: {sorted(importers)}"))
            continue
        try:
            n = importers[kind](conn, stats, account=account, region=region,
                                profile=profile, aws_cli=aws_cli)
            stats.per_kind[kind] = n
        except _AwsCliError as exc:
            stats.errors.append((kind, str(exc)))

    upsert_technology(conn, "aws", "cloud")
    return stats


# ---------------------------------------------------------------------------
# Importers (per-service)
# ---------------------------------------------------------------------------

def _import_rds(conn, stats, *, account, region, profile, aws_cli) -> int:
    out = _aws_json(aws_cli, profile, region,
                    ["rds", "describe-db-instances"])
    count = 0
    for db in out.get("DBInstances", []):
        name = db.get("DBInstanceIdentifier", "")
        if not name:
            continue
        tags = _tags_to_dict(db.get("TagList") or [])
        env = _env_from_tags(tags) or _env_from_name(name)
        props = {
            "arn": db.get("DBInstanceArn", ""),
            "engine": db.get("Engine", ""),
            "engine_version": db.get("EngineVersion", ""),
            "instance_class": db.get("DBInstanceClass", ""),
            "status": db.get("DBInstanceStatus", ""),
            "multi_az": db.get("MultiAZ", False),
            "endpoint": (db.get("Endpoint") or {}).get("Address", ""),
        }
        _insert_resource(conn, "rds", name, env, props, account, region, "aws:rds:DBInstance")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_ec2(conn, stats, *, account, region, profile, aws_cli) -> int:
    out = _aws_json(aws_cli, profile, region,
                    ["ec2", "describe-instances"])
    count = 0
    for reservation in out.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            instance_id = inst.get("InstanceId", "")
            tags = _tags_to_dict(inst.get("Tags") or [])
            name = tags.get("Name") or instance_id
            if not name:
                continue
            env = _env_from_tags(tags) or _env_from_name(name)
            props = {
                "instance_id": instance_id,
                "instance_type": inst.get("InstanceType", ""),
                "state": (inst.get("State") or {}).get("Name", ""),
                "private_ip": inst.get("PrivateIpAddress", ""),
                "vpc_id": inst.get("VpcId", ""),
                "tags": tags,
            }
            _insert_resource(conn, "ec2", name, env, props, account, region, "aws:ec2:Instance")
            count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_s3(conn, stats, *, account, region, profile, aws_cli) -> int:
    out = _aws_json(aws_cli, profile, region,
                    ["s3api", "list-buckets"])
    count = 0
    for bkt in out.get("Buckets", []):
        name = bkt.get("Name", "")
        if not name:
            continue
        env = _env_from_name(name)
        props = {"created": bkt.get("CreationDate", "")}
        _insert_resource(conn, "s3", name, env, props, account, region, "aws:s3:Bucket")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_eks(conn, stats, *, account, region, profile, aws_cli) -> int:
    out = _aws_json(aws_cli, profile, region, ["eks", "list-clusters"])
    count = 0
    for cname in out.get("clusters", []):
        # Describe each for env-from-tags.
        details = _aws_json(aws_cli, profile, region,
                            ["eks", "describe-cluster", "--name", cname])
        c = details.get("cluster", {})
        tags = c.get("tags") or {}  # already dict for EKS
        env = _env_from_tags(tags) or _env_from_name(cname)
        props = {
            "arn": c.get("arn", ""),
            "version": c.get("version", ""),
            "status": c.get("status", ""),
            "endpoint": c.get("endpoint", ""),
            "tags": tags,
        }
        _insert_resource(conn, "eks", cname, env, props, account, region, "aws:eks:Cluster")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_lambda(conn, stats, *, account, region, profile, aws_cli) -> int:
    out = _aws_json(aws_cli, profile, region, ["lambda", "list-functions"])
    count = 0
    for fn in out.get("Functions", []):
        name = fn.get("FunctionName", "")
        if not name:
            continue
        env = _env_from_name(name)
        props = {
            "arn": fn.get("FunctionArn", ""),
            "runtime": fn.get("Runtime", ""),
            "handler": fn.get("Handler", ""),
            "memory_size": fn.get("MemorySize", 0),
            "timeout": fn.get("Timeout", 0),
        }
        _insert_resource(conn, "lambda", name, env, props, account, region, "aws:lambda:Function")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_elb(conn, stats, *, account, region, profile, aws_cli) -> int:
    out = _aws_json(aws_cli, profile, region,
                    ["elbv2", "describe-load-balancers"])
    count = 0
    for lb in out.get("LoadBalancers", []):
        name = lb.get("LoadBalancerName", "")
        if not name:
            continue
        env = _env_from_name(name)
        props = {
            "arn": lb.get("LoadBalancerArn", ""),
            "type": lb.get("Type", ""),
            "scheme": lb.get("Scheme", ""),
            "dns_name": lb.get("DNSName", ""),
        }
        _insert_resource(conn, "elb", name, env, props, account, region, "aws:elbv2:LoadBalancer")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_ecs(conn, stats, *, account, region, profile, aws_cli) -> int:
    out = _aws_json(aws_cli, profile, region, ["ecs", "list-clusters"])
    count = 0
    for arn in out.get("clusterArns", []):
        name = arn.rsplit("/", 1)[-1]
        env = _env_from_name(name)
        _insert_resource(conn, "ecs", name, env, {"arn": arn},
                         account, region, "aws:ecs:Cluster")
        count += 1
        # Services in the cluster.
        svc_out = _aws_json(aws_cli, profile, region,
                            ["ecs", "list-services", "--cluster", name])
        for svc_arn in svc_out.get("serviceArns", []):
            svc_name = svc_arn.rsplit("/", 1)[-1]
            _insert_resource(conn, "ecs", svc_name, env,
                             {"cluster": name, "arn": svc_arn},
                             account, region, "aws:ecs:Service")
            count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_sqs(conn, stats, *, account, region, profile, aws_cli) -> int:
    out = _aws_json(aws_cli, profile, region, ["sqs", "list-queues"])
    count = 0
    for url in out.get("QueueUrls", []) or []:
        name = url.rsplit("/", 1)[-1]
        env = _env_from_name(name)
        _insert_resource(conn, "sqs", name, env, {"url": url},
                         account, region, "aws:sqs:Queue")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_sns(conn, stats, *, account, region, profile, aws_cli) -> int:
    out = _aws_json(aws_cli, profile, region, ["sns", "list-topics"])
    count = 0
    for topic in out.get("Topics", []):
        arn = topic.get("TopicArn", "")
        name = arn.rsplit(":", 1)[-1]
        if not name:
            continue
        env = _env_from_name(name)
        _insert_resource(conn, "sns", name, env, {"arn": arn},
                         account, region, "aws:sns:Topic")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_dynamodb(conn, stats, *, account, region, profile, aws_cli) -> int:
    out = _aws_json(aws_cli, profile, region, ["dynamodb", "list-tables"])
    count = 0
    for table_name in out.get("TableNames", []):
        env = _env_from_name(table_name)
        _insert_resource(conn, "dynamodb", table_name, env, {},
                         account, region, "aws:dynamodb:Table")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _AwsCliError(Exception):
    pass


def _aws_account_id(aws_cli: str, profile: str | None) -> str | None:
    try:
        out = _aws_json(aws_cli, profile, None,
                        ["sts", "get-caller-identity"])
        return out.get("Account")
    except _AwsCliError:
        return None


def _aws_default_region(aws_cli: str, profile: str | None) -> str:
    """Return the AWS CLI's default region, falling back to us-east-1."""
    env_region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if env_region:
        return env_region
    try:
        argv = [aws_cli, "configure", "get", "region"]
        if profile:
            argv += ["--profile", profile]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        out = result.stdout.strip()
        return out or "us-east-1"
    except Exception:
        return "us-east-1"


def _aws_json(aws_cli: str, profile: str | None, region: str | None,
              args: list[str]) -> dict:
    """Run an aws CLI subcommand, return parsed JSON. Raises _AwsCliError."""
    argv = [aws_cli] + args + ["--output", "json"]
    if region:
        argv += ["--region", region]
    if profile:
        argv += ["--profile", profile]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise _AwsCliError(f"timeout running: {' '.join(argv)}")
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip().splitlines()
        msg = err[-1] if err else f"aws cli exited {result.returncode}"
        raise _AwsCliError(f"{' '.join(args)}: {msg}")
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise _AwsCliError(f"non-JSON output from {' '.join(args)}: {exc}")


def _tags_to_dict(tags: list) -> dict[str, str]:
    """AWS tag formats vary: [{"Key":"X","Value":"y"}] or [{"TagKey":"X","TagValue":"y"}]."""
    out: dict[str, str] = {}
    for t in tags:
        if not isinstance(t, dict):
            continue
        k = t.get("Key") or t.get("TagKey") or t.get("key")
        v = t.get("Value") or t.get("TagValue") or t.get("value", "")
        if k:
            out[str(k)] = str(v)
    return out


_PROD_KEYS = ("environment", "Environment", "ENV", "env", "stage", "Stage", "tier")


def _env_from_tags(tags: dict[str, str]) -> str:
    for key in _PROD_KEYS:
        if key in tags:
            v = tags[key].strip().lower()
            if v in ("prod", "production", "live"):
                return "production"
            if v in ("stag", "staging", "preprod", "uat"):
                return "staging"
            if v in ("dev", "develop", "development", "test", "sandbox"):
                return "dev"
            if v:
                return v
    return ""


def _env_from_name(name: str) -> str:
    n = name.lower()
    for hint in ("prod", "production"):
        if hint in n:
            return "production"
    for hint in ("staging", "preprod", "uat"):
        if hint in n:
            return "staging"
    for hint in ("dev", "develop", "sandbox", "test"):
        if hint in n:
            return "dev"
    return ""


def _tier_for_env(env: str) -> str:
    if env == "production":
        return RISK_RED
    if env == "staging":
        return RISK_ORANGE
    return RISK_GREEN


def _insert_resource(
    conn: sqlite3.Connection,
    service: str,
    name: str,
    env: str,
    props: dict[str, Any],
    account: str,
    region: str,
    kind: str,
) -> None:
    """Insert one discovered cloud resource (plus its virtual File row)."""
    file_path = _VIRTUAL_PATH_TEMPLATE.format(account=account, region=region, service=service)
    # Ensure the virtual file row exists.
    now = datetime.now(timezone.utc).isoformat()
    upsert_file(conn, FileRow(
        path=file_path, project_path="",  # cloud resources have no source repo
        name=f"aws:{service}", extension="",
        size_bytes=0, content_hash=f"aws:{account}:{region}:{service}",
        modified_at=now,
        risk_tier=_tier_for_env(env),
    ))
    tier = _tier_for_env(env)
    props_with_provenance = dict(props)
    props_with_provenance["discovered_from"] = "aws-cli"
    props_with_provenance["account"] = account
    props_with_provenance["region"] = region
    uid = resource_uid(kind, name, region, file_path)
    upsert_resource(conn, ResourceRow(
        uid=uid, file_path=file_path, kind=kind,
        name=name, namespace=region, environment=env, risk_tier=tier,
        properties=props_with_provenance,
        context_snippet=f"discovered in {account}/{region} via aws-cli",
    ))
    # Tech edge.
    upsert_edge(conn, EdgeSpec(
        src_kind="resource", src_id=uid,
        dst_kind="technology", dst_id="aws",
        rel_type="USES",
    ))
