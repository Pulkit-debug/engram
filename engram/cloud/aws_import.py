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
        # Original 10 (v0.2)
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
        # v0.3: network + access + secrets surface
        "vpc":      _import_vpc,
        "subnet":   _import_subnet,
        "sg":       _import_security_groups,
        "iam":      _import_iam,
        "secrets":  _import_secrets,
        "route53":  _import_route53,
        # v0.4: customer-facing + state + security
        "cloudfront":  _import_cloudfront,
        "apigateway":  _import_apigateway,
        "asg":         _import_asg,
        "ebs":         _import_ebs,
        "elasticache": _import_elasticache,
        "logs":        _import_cloudwatch_logs,
        "alarms":      _import_cloudwatch_alarms,
        "events":      _import_eventbridge,
        "stepfunctions": _import_stepfunctions,
        "kms":         _import_kms,
        "acm":         _import_acm,
        "cognito":     _import_cognito,
        "kinesis":     _import_kinesis,
        "opensearch":  _import_opensearch,
        "redshift":    _import_redshift,
        "waf":         _import_waf,
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

    # Post-pass: wire USES edges from captured _attachments to the
    # corresponding SG / Subnet / VPC / IAM Resources, if any of them
    # were also discovered in this run (or were already in the graph).
    try:
        from engram.inference.cloud_attachments import link_cloud_attachments
        link_cloud_attachments(conn)
    except Exception as exc:
        logger.warning("cloud-attachment linking failed: %s", exc)

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
        # Capture attachment IDs so _link_attachments() can wire USES edges.
        sg_ids = [g.get("VpcSecurityGroupId", "") for g in (db.get("VpcSecurityGroups") or [])]
        subnet_ids = [
            s.get("SubnetIdentifier", "")
            for s in ((db.get("DBSubnetGroup") or {}).get("Subnets") or [])
        ]
        props = {
            "arn": db.get("DBInstanceArn", ""),
            "engine": db.get("Engine", ""),
            "engine_version": db.get("EngineVersion", ""),
            "instance_class": db.get("DBInstanceClass", ""),
            "status": db.get("DBInstanceStatus", ""),
            "multi_az": db.get("MultiAZ", False),
            "endpoint": (db.get("Endpoint") or {}).get("Address", ""),
            "_attachments": {
                "security_group_ids": [s for s in sg_ids if s],
                "subnet_ids": [s for s in subnet_ids if s],
                "db_subnet_group": (db.get("DBSubnetGroup") or {}).get("DBSubnetGroupName", ""),
            },
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
            sg_ids = [g.get("GroupId", "") for g in (inst.get("SecurityGroups") or [])]
            iam_profile = (inst.get("IamInstanceProfile") or {}).get("Arn", "")
            props = {
                "instance_id": instance_id,
                "instance_type": inst.get("InstanceType", ""),
                "state": (inst.get("State") or {}).get("Name", ""),
                "private_ip": inst.get("PrivateIpAddress", ""),
                "vpc_id": inst.get("VpcId", ""),
                "subnet_id": inst.get("SubnetId", ""),
                "tags": tags,
                "_attachments": {
                    "security_group_ids": [s for s in sg_ids if s],
                    "subnet_ids": [inst.get("SubnetId", "")] if inst.get("SubnetId") else [],
                    "vpc_ids": [inst.get("VpcId", "")] if inst.get("VpcId") else [],
                    "iam_instance_profile_arn": iam_profile,
                },
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
# v0.3 — Network + access + secrets surface
# ---------------------------------------------------------------------------

def _import_vpc(conn, stats, *, account, region, profile, aws_cli) -> int:
    """VPCs. The top of the network blast-radius tree."""
    out = _aws_json(aws_cli, profile, region, ["ec2", "describe-vpcs"])
    count = 0
    for vpc in out.get("Vpcs", []):
        vpc_id = vpc.get("VpcId", "")
        tags = _tags_to_dict(vpc.get("Tags") or [])
        name = tags.get("Name") or vpc_id
        if not name:
            continue
        env = _env_from_tags(tags) or _env_from_name(name)
        props = {
            "vpc_id": vpc_id,
            "cidr_block": vpc.get("CidrBlock", ""),
            "is_default": vpc.get("IsDefault", False),
            "state": vpc.get("State", ""),
            "tags": tags,
        }
        _insert_resource(conn, "vpc", name, env, props, account, region, "aws:ec2:VPC")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_subnet(conn, stats, *, account, region, profile, aws_cli) -> int:
    out = _aws_json(aws_cli, profile, region, ["ec2", "describe-subnets"])
    count = 0
    for sn in out.get("Subnets", []):
        subnet_id = sn.get("SubnetId", "")
        tags = _tags_to_dict(sn.get("Tags") or [])
        name = tags.get("Name") or subnet_id
        if not name:
            continue
        env = _env_from_tags(tags) or _env_from_name(name)
        props = {
            "subnet_id": subnet_id,
            "vpc_id": sn.get("VpcId", ""),
            "cidr_block": sn.get("CidrBlock", ""),
            "availability_zone": sn.get("AvailabilityZone", ""),
            "tags": tags,
        }
        _insert_resource(conn, "subnet", name, env, props, account, region, "aws:ec2:Subnet")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_security_groups(conn, stats, *, account, region, profile, aws_cli) -> int:
    """Security groups. Deleting one orphans every service depending on it."""
    out = _aws_json(aws_cli, profile, region, ["ec2", "describe-security-groups"])
    count = 0
    for sg in out.get("SecurityGroups", []):
        sg_id = sg.get("GroupId", "")
        sg_name = sg.get("GroupName", "")
        tags = _tags_to_dict(sg.get("Tags") or [])
        # Prefer Name tag, then GroupName, then GroupId.
        name = tags.get("Name") or sg_name or sg_id
        if not name:
            continue
        env = _env_from_tags(tags) or _env_from_name(name)
        # Summarize ingress / egress rules count (full rules are noisy in props).
        ingress = sg.get("IpPermissions") or []
        egress = sg.get("IpPermissionsEgress") or []
        props = {
            "group_id": sg_id,
            "group_name": sg_name,
            "vpc_id": sg.get("VpcId", ""),
            "description": sg.get("Description", ""),
            "ingress_rule_count": len(ingress),
            "egress_rule_count": len(egress),
            "tags": tags,
        }
        _insert_resource(conn, "sg", name, env, props,
                         account, region, "aws:ec2:SecurityGroup")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_iam(conn, stats, *, account, region, profile, aws_cli) -> int:
    """IAM roles. (Region-independent — IAM is global; we use 'global' as namespace.)"""
    # IAM is a global service; the --region argument is meaningless here.
    out = _aws_json(aws_cli, profile, None, ["iam", "list-roles"])
    count = 0
    for role in out.get("Roles", []):
        name = role.get("RoleName", "")
        if not name:
            continue
        env = _env_from_name(name)
        props = {
            "arn": role.get("Arn", ""),
            "role_id": role.get("RoleId", ""),
            "path": role.get("Path", ""),
            "create_date": role.get("CreateDate", ""),
        }
        # Use 'global' as the region/namespace marker for IAM.
        _insert_resource(conn, "iam", name, env, props,
                         account, "global", "aws:iam:Role")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_secrets(conn, stats, *, account, region, profile, aws_cli) -> int:
    """SecretsManager + SSM Parameter Store — the AWS-native env-var equivalent."""
    count = 0

    # SecretsManager.
    out = _aws_json(aws_cli, profile, region,
                    ["secretsmanager", "list-secrets"])
    for sec in out.get("SecretList", []):
        name = sec.get("Name", "")
        if not name:
            continue
        env = _env_from_name(name)
        props = {
            "arn": sec.get("ARN", ""),
            "description": sec.get("Description", ""),
            "last_changed": sec.get("LastChangedDate", ""),
            "tags": _tags_to_dict(sec.get("Tags") or []),
        }
        _insert_resource(conn, "secrets", name, env, props,
                         account, region, "aws:secretsmanager:Secret")
        count += 1

    # SSM Parameter Store (best-effort; some accounts won't have any).
    try:
        out = _aws_json(aws_cli, profile, region,
                        ["ssm", "describe-parameters"])
        for p in out.get("Parameters", []):
            name = p.get("Name", "")
            if not name:
                continue
            env = _env_from_name(name)
            props = {
                "type": p.get("Type", ""),
                "tier": p.get("Tier", ""),
                "last_modified": p.get("LastModifiedDate", ""),
                "version": p.get("Version", 0),
            }
            _insert_resource(conn, "secrets", name, env, props,
                             account, region, "aws:ssm:Parameter")
            count += 1
    except _AwsCliError:
        # SSM may not be available; not fatal.
        pass

    stats.discovered += count
    stats.inserted += count
    return count


def _import_route53(conn, stats, *, account, region, profile, aws_cli) -> int:
    """Route53 zones + record sets. DNS is the biggest blind spot in click-ops shops.

    Endpoints from RDS / ELB / CloudFront ultimately flow through Route53 records.
    Indexing them lets value_match.py connect env-var hostnames to the resources
    behind them.
    """
    # Route53 is global; the --region argument is ignored.
    out = _aws_json(aws_cli, profile, None, ["route53", "list-hosted-zones"])
    count = 0
    for zone in out.get("HostedZones", []):
        zone_id = zone.get("Id", "").rsplit("/", 1)[-1]
        name = (zone.get("Name") or "").rstrip(".")
        if not name:
            continue
        env = _env_from_name(name)
        zone_props = {
            "zone_id": zone_id,
            "private_zone": zone.get("Config", {}).get("PrivateZone", False),
            "record_count": zone.get("ResourceRecordSetCount", 0),
        }
        _insert_resource(conn, "route53", name, env, zone_props,
                         account, "global", "aws:route53:HostedZone")
        count += 1

        # Drill into record sets (best-effort; can be slow on large zones).
        try:
            rs_out = _aws_json(
                aws_cli, profile, None,
                ["route53", "list-resource-record-sets", "--hosted-zone-id", zone_id],
            )
            for rrset in rs_out.get("ResourceRecordSets", []):
                rec_name = (rrset.get("Name") or "").rstrip(".")
                rec_type = rrset.get("Type", "")
                if not rec_name or rec_type not in ("A", "AAAA", "CNAME", "ALIAS"):
                    continue
                # Pull the actual values — these are what env vars match against.
                values: list[str] = []
                for rr in rrset.get("ResourceRecords") or []:
                    if isinstance(rr, dict) and rr.get("Value"):
                        values.append(rr["Value"])
                alias_target = rrset.get("AliasTarget")
                if alias_target and alias_target.get("DNSName"):
                    values.append(alias_target["DNSName"].rstrip("."))
                record_env = _env_from_name(rec_name)
                rec_props = {
                    "record_type": rec_type,
                    "ttl": rrset.get("TTL", 0),
                    "dns_name": rec_name,
                    "values": values,
                    "alias": bool(alias_target),
                }
                # Critically: store the *value* (target hostname) in dns_name
                # so value_match.py can link env vars pointing at it.
                _insert_resource(conn, "route53", rec_name, record_env, rec_props,
                                 account, "global", f"aws:route53:RecordSet:{rec_type}")
                count += 1
        except _AwsCliError:
            pass

    stats.discovered += count
    stats.inserted += count
    return count


# ---------------------------------------------------------------------------
# v0.4 — Customer-facing surface + state + security
# ---------------------------------------------------------------------------

def _import_cloudfront(conn, stats, *, account, region, profile, aws_cli) -> int:
    """CloudFront distributions. Customer-facing CDN. Global service."""
    out = _aws_json(aws_cli, profile, None, ["cloudfront", "list-distributions"])
    dlist = (out.get("DistributionList") or {}).get("Items") or []
    count = 0
    for d in dlist:
        dist_id = d.get("Id", "")
        name = d.get("DomainName", "") or dist_id
        if not name:
            continue
        env = _env_from_name(name)
        props = {
            "id": dist_id,
            "arn": d.get("ARN", ""),
            "domain_name": d.get("DomainName", ""),
            "status": d.get("Status", ""),
            "enabled": d.get("Enabled", False),
            "price_class": d.get("PriceClass", ""),
        }
        _insert_resource(conn, "cloudfront", name, env, props,
                         account, "global", "aws:cloudfront:Distribution")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_apigateway(conn, stats, *, account, region, profile, aws_cli) -> int:
    """API Gateway REST APIs + HTTP APIs (v2)."""
    count = 0
    # REST APIs.
    try:
        rest_out = _aws_json(aws_cli, profile, region, ["apigateway", "get-rest-apis"])
        for api in rest_out.get("items", []):
            name = api.get("name", "") or api.get("id", "")
            if not name:
                continue
            env = _env_from_name(name)
            props = {
                "id": api.get("id", ""),
                "endpoint_configuration": api.get("endpointConfiguration", {}),
                "created_date": api.get("createdDate", ""),
            }
            _insert_resource(conn, "apigateway", name, env, props,
                             account, region, "aws:apigateway:RestApi")
            count += 1
    except _AwsCliError:
        pass

    # HTTP / WebSocket APIs.
    try:
        v2_out = _aws_json(aws_cli, profile, region, ["apigatewayv2", "get-apis"])
        for api in v2_out.get("Items", []):
            name = api.get("Name", "") or api.get("ApiId", "")
            if not name:
                continue
            env = _env_from_name(name)
            props = {
                "id": api.get("ApiId", ""),
                "protocol_type": api.get("ProtocolType", ""),
                "api_endpoint": api.get("ApiEndpoint", ""),
            }
            _insert_resource(conn, "apigateway", name, env, props,
                             account, region, "aws:apigatewayv2:Api")
            count += 1
    except _AwsCliError:
        pass

    stats.discovered += count
    stats.inserted += count
    return count


def _import_asg(conn, stats, *, account, region, profile, aws_cli) -> int:
    """Auto Scaling Groups + Launch Templates."""
    count = 0
    # ASGs.
    try:
        out = _aws_json(aws_cli, profile, region,
                        ["autoscaling", "describe-auto-scaling-groups"])
        for asg in out.get("AutoScalingGroups", []):
            name = asg.get("AutoScalingGroupName", "")
            if not name:
                continue
            tags = _tags_to_dict(asg.get("Tags") or [])
            env = _env_from_tags(tags) or _env_from_name(name)
            props = {
                "arn": asg.get("AutoScalingGroupARN", ""),
                "desired": asg.get("DesiredCapacity", 0),
                "min": asg.get("MinSize", 0),
                "max": asg.get("MaxSize", 0),
                "launch_template": (asg.get("LaunchTemplate") or {}).get("LaunchTemplateName", ""),
                "vpc_zone_identifier": asg.get("VPCZoneIdentifier", ""),
                "tags": tags,
            }
            _insert_resource(conn, "asg", name, env, props,
                             account, region, "aws:autoscaling:Group")
            count += 1
    except _AwsCliError:
        pass

    # Launch Templates.
    try:
        out = _aws_json(aws_cli, profile, region,
                        ["ec2", "describe-launch-templates"])
        for lt in out.get("LaunchTemplates", []):
            name = lt.get("LaunchTemplateName", "") or lt.get("LaunchTemplateId", "")
            if not name:
                continue
            tags = _tags_to_dict(lt.get("Tags") or [])
            env = _env_from_tags(tags) or _env_from_name(name)
            props = {
                "id": lt.get("LaunchTemplateId", ""),
                "default_version": lt.get("DefaultVersionNumber", 0),
                "latest_version": lt.get("LatestVersionNumber", 0),
                "tags": tags,
            }
            _insert_resource(conn, "asg", name, env, props,
                             account, region, "aws:ec2:LaunchTemplate")
            count += 1
    except _AwsCliError:
        pass

    stats.discovered += count
    stats.inserted += count
    return count


def _import_ebs(conn, stats, *, account, region, profile, aws_cli) -> int:
    """EBS Volumes — holds state independently from EC2."""
    out = _aws_json(aws_cli, profile, region, ["ec2", "describe-volumes"])
    count = 0
    for vol in out.get("Volumes", []):
        vol_id = vol.get("VolumeId", "")
        tags = _tags_to_dict(vol.get("Tags") or [])
        name = tags.get("Name") or vol_id
        if not name:
            continue
        env = _env_from_tags(tags) or _env_from_name(name)
        attachments = vol.get("Attachments") or []
        props = {
            "volume_id": vol_id,
            "size_gb": vol.get("Size", 0),
            "volume_type": vol.get("VolumeType", ""),
            "state": vol.get("State", ""),
            "encrypted": vol.get("Encrypted", False),
            "attached_instance": attachments[0].get("InstanceId", "") if attachments else "",
            "availability_zone": vol.get("AvailabilityZone", ""),
            "tags": tags,
        }
        _insert_resource(conn, "ebs", name, env, props,
                         account, region, "aws:ec2:Volume")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_elasticache(conn, stats, *, account, region, profile, aws_cli) -> int:
    """ElastiCache: Redis + Memcached."""
    count = 0
    # CacheClusters (Memcached + Redis non-cluster-mode).
    try:
        out = _aws_json(aws_cli, profile, region,
                        ["elasticache", "describe-cache-clusters"])
        for cluster in out.get("CacheClusters", []):
            name = cluster.get("CacheClusterId", "")
            if not name:
                continue
            env = _env_from_name(name)
            props = {
                "arn": cluster.get("ARN", ""),
                "engine": cluster.get("Engine", ""),
                "engine_version": cluster.get("EngineVersion", ""),
                "node_type": cluster.get("CacheNodeType", ""),
                "num_nodes": cluster.get("NumCacheNodes", 0),
                "status": cluster.get("CacheClusterStatus", ""),
                "endpoint": (cluster.get("ConfigurationEndpoint") or {}).get("Address", ""),
            }
            _insert_resource(conn, "elasticache", name, env, props,
                             account, region, "aws:elasticache:CacheCluster")
            count += 1
    except _AwsCliError:
        pass

    # Replication Groups (Redis cluster mode).
    try:
        out = _aws_json(aws_cli, profile, region,
                        ["elasticache", "describe-replication-groups"])
        for rg in out.get("ReplicationGroups", []):
            name = rg.get("ReplicationGroupId", "")
            if not name:
                continue
            env = _env_from_name(name)
            props = {
                "arn": rg.get("ARN", ""),
                "description": rg.get("Description", ""),
                "status": rg.get("Status", ""),
                "endpoint": (rg.get("ConfigurationEndpoint") or {}).get("Address", ""),
                "cluster_enabled": rg.get("ClusterEnabled", False),
            }
            _insert_resource(conn, "elasticache", name, env, props,
                             account, region, "aws:elasticache:ReplicationGroup")
            count += 1
    except _AwsCliError:
        pass

    stats.discovered += count
    stats.inserted += count
    return count


def _import_cloudwatch_logs(conn, stats, *, account, region, profile, aws_cli) -> int:
    """CloudWatch Log Groups."""
    out = _aws_json(aws_cli, profile, region, ["logs", "describe-log-groups"])
    count = 0
    for lg in out.get("logGroups", []):
        name = lg.get("logGroupName", "")
        if not name:
            continue
        env = _env_from_name(name)
        props = {
            "arn": lg.get("arn", ""),
            "retention_days": lg.get("retentionInDays", 0),
            "stored_bytes": lg.get("storedBytes", 0),
            "kms_key_id": lg.get("kmsKeyId", ""),
        }
        _insert_resource(conn, "logs", name, env, props,
                         account, region, "aws:logs:LogGroup")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_cloudwatch_alarms(conn, stats, *, account, region, profile, aws_cli) -> int:
    """CloudWatch metric alarms. Deleting these hides incidents."""
    out = _aws_json(aws_cli, profile, region, ["cloudwatch", "describe-alarms"])
    count = 0
    for a in out.get("MetricAlarms", []):
        name = a.get("AlarmName", "")
        if not name:
            continue
        env = _env_from_name(name)
        props = {
            "arn": a.get("AlarmArn", ""),
            "state": a.get("StateValue", ""),
            "metric_name": a.get("MetricName", ""),
            "namespace": a.get("Namespace", ""),
            "threshold": a.get("Threshold", 0),
            "comparison": a.get("ComparisonOperator", ""),
            "actions_enabled": a.get("ActionsEnabled", False),
        }
        _insert_resource(conn, "alarms", name, env, props,
                         account, region, "aws:cloudwatch:Alarm")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_eventbridge(conn, stats, *, account, region, profile, aws_cli) -> int:
    """EventBridge rules. Async fan-out; deletion cascades."""
    out = _aws_json(aws_cli, profile, region, ["events", "list-rules"])
    count = 0
    for rule in out.get("Rules", []):
        name = rule.get("Name", "")
        if not name:
            continue
        env = _env_from_name(name)
        props = {
            "arn": rule.get("Arn", ""),
            "state": rule.get("State", ""),
            "schedule_expression": rule.get("ScheduleExpression", ""),
            "event_pattern": rule.get("EventPattern", ""),
        }
        _insert_resource(conn, "events", name, env, props,
                         account, region, "aws:events:Rule")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_stepfunctions(conn, stats, *, account, region, profile, aws_cli) -> int:
    """Step Functions state machines. Orchestration; high-impact deletion."""
    out = _aws_json(aws_cli, profile, region, ["stepfunctions", "list-state-machines"])
    count = 0
    for sm in out.get("stateMachines", []):
        name = sm.get("name", "")
        if not name:
            continue
        env = _env_from_name(name)
        props = {
            "arn": sm.get("stateMachineArn", ""),
            "type": sm.get("type", ""),
            "creation_date": sm.get("creationDate", ""),
        }
        _insert_resource(conn, "stepfunctions", name, env, props,
                         account, region, "aws:states:StateMachine")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_kms(conn, stats, *, account, region, profile, aws_cli) -> int:
    """KMS keys. Deleting one renders data unreadable. CRITICAL."""
    out = _aws_json(aws_cli, profile, region, ["kms", "list-keys"])
    count = 0
    for key in out.get("Keys", []):
        key_id = key.get("KeyId", "")
        if not key_id:
            continue
        # Most KMS keys don't have a friendly name; pull aliases best-effort.
        name = key_id
        env = ""
        try:
            aliases = _aws_json(aws_cli, profile, region,
                                ["kms", "list-aliases", "--key-id", key_id])
            alias_list = aliases.get("Aliases") or []
            if alias_list:
                alias_name = alias_list[0].get("AliasName", "") or ""
                if alias_name.startswith("alias/"):
                    alias_name = alias_name[6:]
                if alias_name:
                    name = alias_name
                    env = _env_from_name(name)
        except _AwsCliError:
            pass

        props = {
            "key_id": key_id,
            "arn": key.get("KeyArn", ""),
        }
        _insert_resource(conn, "kms", name, env, props,
                         account, region, "aws:kms:Key")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_acm(conn, stats, *, account, region, profile, aws_cli) -> int:
    """ACM certificates. Deleting one breaks HTTPS for whatever uses it."""
    out = _aws_json(aws_cli, profile, region, ["acm", "list-certificates"])
    count = 0
    for cert in out.get("CertificateSummaryList", []):
        domain = cert.get("DomainName", "") or cert.get("CertificateArn", "")
        if not domain:
            continue
        env = _env_from_name(domain)
        props = {
            "arn": cert.get("CertificateArn", ""),
            "domain_name": cert.get("DomainName", ""),
            "status": cert.get("Status", ""),
            "type": cert.get("Type", ""),
            "in_use_by": cert.get("InUse", False),
        }
        _insert_resource(conn, "acm", domain, env, props,
                         account, region, "aws:acm:Certificate")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_cognito(conn, stats, *, account, region, profile, aws_cli) -> int:
    """Cognito User Pools. Auth surface; deleting = users locked out."""
    out = _aws_json(aws_cli, profile, region,
                    ["cognito-idp", "list-user-pools", "--max-results", "60"])
    count = 0
    for pool in out.get("UserPools", []):
        name = pool.get("Name", "") or pool.get("Id", "")
        if not name:
            continue
        env = _env_from_name(name)
        props = {
            "id": pool.get("Id", ""),
            "creation_date": pool.get("CreationDate", ""),
            "last_modified_date": pool.get("LastModifiedDate", ""),
        }
        _insert_resource(conn, "cognito", name, env, props,
                         account, region, "aws:cognito:UserPool")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_kinesis(conn, stats, *, account, region, profile, aws_cli) -> int:
    """Kinesis Data Streams."""
    out = _aws_json(aws_cli, profile, region, ["kinesis", "list-streams"])
    count = 0
    for stream_name in out.get("StreamNames", []):
        env = _env_from_name(stream_name)
        # Could describe-stream for shard count; keeping it cheap.
        _insert_resource(conn, "kinesis", stream_name, env, {},
                         account, region, "aws:kinesis:Stream")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_opensearch(conn, stats, *, account, region, profile, aws_cli) -> int:
    """OpenSearch / Elasticsearch domains."""
    out = _aws_json(aws_cli, profile, region,
                    ["opensearch", "list-domain-names"])
    count = 0
    for d in out.get("DomainNames", []):
        name = d.get("DomainName", "")
        if not name:
            continue
        env = _env_from_name(name)
        engine_type = d.get("EngineType", "")
        props = {"engine_type": engine_type}
        _insert_resource(conn, "opensearch", name, env, props,
                         account, region, "aws:opensearch:Domain")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_redshift(conn, stats, *, account, region, profile, aws_cli) -> int:
    """Redshift clusters. Data warehouse."""
    out = _aws_json(aws_cli, profile, region,
                    ["redshift", "describe-clusters"])
    count = 0
    for cluster in out.get("Clusters", []):
        name = cluster.get("ClusterIdentifier", "")
        if not name:
            continue
        tags = _tags_to_dict(cluster.get("Tags") or [])
        env = _env_from_tags(tags) or _env_from_name(name)
        props = {
            "node_type": cluster.get("NodeType", ""),
            "status": cluster.get("ClusterStatus", ""),
            "num_nodes": cluster.get("NumberOfNodes", 0),
            "endpoint": (cluster.get("Endpoint") or {}).get("Address", ""),
            "encrypted": cluster.get("Encrypted", False),
            "tags": tags,
        }
        _insert_resource(conn, "redshift", name, env, props,
                         account, region, "aws:redshift:Cluster")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_waf(conn, stats, *, account, region, profile, aws_cli) -> int:
    """WAFv2 WebACLs. Security policies; removing one opens a hole."""
    count = 0
    for scope in ("REGIONAL", "CLOUDFRONT"):
        scope_region = None if scope == "CLOUDFRONT" else region
        try:
            out = _aws_json(
                aws_cli, profile, scope_region,
                ["wafv2", "list-web-acls", "--scope", scope],
            )
        except _AwsCliError:
            continue
        for acl in out.get("WebACLs", []):
            name = acl.get("Name", "")
            if not name:
                continue
            env = _env_from_name(name)
            props = {
                "arn": acl.get("ARN", ""),
                "id": acl.get("Id", ""),
                "scope": scope,
            }
            _insert_resource(conn, "waf", name, env, props,
                             account, scope_region or "global",
                             "aws:wafv2:WebACL")
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
