"""GCP resource discovery via the gcloud CLI.

Mirrors aws_import.py: shells out to `gcloud`, parses JSON, inserts
Resources with `discovered_from = 'gcloud-cli'`. Engram never authenticates
to GCP.

Supported kinds (v0.4):
  compute    — Compute Engine instances + disks
  storage    — Cloud Storage buckets
  sql        — Cloud SQL instances
  gke        — GKE clusters + node pools
  functions  — Cloud Functions
  run        — Cloud Run services
  pubsub     — Pub/Sub topics + subscriptions
  bigquery   — BigQuery datasets + tables
  iam        — Service Accounts
  network    — VPC networks + subnetworks + firewall rules
  dns        — Cloud DNS managed zones
  secrets    — Secret Manager + Memorystore
"""

from __future__ import annotations

import json
import logging
import shutil
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

_VIRTUAL_PATH_TEMPLATE = "gcp://{project}/{region}/{service}"


@dataclass
class CloudImportStats:
    discovered: int = 0
    inserted: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    per_kind: dict[str, int] = field(default_factory=dict)


def import_gcp(
    conn,
    *,
    kinds: list[str],
    project: str | None = None,
    region: str | None = None,
    gcloud_cli: str = "gcloud",
) -> CloudImportStats:
    """Discover GCP resources and insert them into the graph."""
    stats = CloudImportStats()

    if not shutil.which(gcloud_cli):
        stats.errors.append(("__cli__", f"gcloud CLI not found at '{gcloud_cli}'. "
                             "Install: https://cloud.google.com/sdk/docs/install"))
        return stats

    project = project or _gcloud_default_project(gcloud_cli)
    if not project:
        stats.errors.append(("__cli__",
            "No GCP project set. Run `gcloud config set project <id>` or pass --project."))
        return stats

    importers: dict[str, Callable] = {
        "compute":   _import_compute,
        "storage":   _import_storage,
        "sql":       _import_sql,
        "gke":       _import_gke,
        "functions": _import_functions,
        "run":       _import_run,
        "pubsub":    _import_pubsub,
        "bigquery":  _import_bigquery,
        "iam":       _import_iam,
        "network":   _import_network,
        "dns":       _import_dns,
        "secrets":   _import_secrets,
    }
    for kind in kinds:
        if kind not in importers:
            stats.errors.append((kind, f"unknown kind. Supported: {sorted(importers)}"))
            continue
        try:
            n = importers[kind](conn, stats, project=project,
                                region=region, gcloud_cli=gcloud_cli)
            stats.per_kind[kind] = n
        except _GcloudError as exc:
            stats.errors.append((kind, str(exc)))

    upsert_technology(conn, "gcp", "cloud")

    # Cross-resource edges, same post-pass as AWS.
    try:
        from engram.inference.cloud_attachments import link_cloud_attachments
        link_cloud_attachments(conn)
    except Exception as exc:
        logger.warning("cloud-attachment linking (gcp) failed: %s", exc)

    return stats


# ---------------------------------------------------------------------------
# Per-service
# ---------------------------------------------------------------------------

def _import_compute(conn, stats, *, project, region, gcloud_cli) -> int:
    """Compute Engine instances + disks."""
    count = 0
    instances = _gcloud_json(gcloud_cli, project,
                             ["compute", "instances", "list"])
    for inst in instances:
        name = inst.get("name", "")
        if not name:
            continue
        labels = inst.get("labels") or {}
        env = _env_from_labels(labels) or _env_from_name(name)
        zone = (inst.get("zone") or "").rsplit("/", 1)[-1]
        props = {
            "machine_type": (inst.get("machineType") or "").rsplit("/", 1)[-1],
            "status": inst.get("status", ""),
            "zone": zone,
            "internal_ip": _gcp_first_internal_ip(inst),
            "labels": labels,
        }
        _insert_resource(conn, "compute", name, env, props,
                         project, zone or "global",
                         "gcp:compute:Instance")
        count += 1

    disks = _gcloud_json(gcloud_cli, project,
                        ["compute", "disks", "list"])
    for d in disks:
        name = d.get("name", "")
        if not name:
            continue
        labels = d.get("labels") or {}
        env = _env_from_labels(labels) or _env_from_name(name)
        zone = (d.get("zone") or "").rsplit("/", 1)[-1]
        props = {
            "size_gb": d.get("sizeGb", "0"),
            "type": (d.get("type") or "").rsplit("/", 1)[-1],
            "status": d.get("status", ""),
            "labels": labels,
        }
        _insert_resource(conn, "compute", name, env, props,
                         project, zone or "global",
                         "gcp:compute:Disk")
        count += 1

    stats.discovered += count
    stats.inserted += count
    return count


def _import_storage(conn, stats, *, project, region, gcloud_cli) -> int:
    """Cloud Storage buckets."""
    buckets = _gcloud_json(gcloud_cli, project, ["storage", "buckets", "list"])
    count = 0
    for b in buckets:
        name = b.get("name", "")
        if not name:
            continue
        labels = b.get("labels") or {}
        env = _env_from_labels(labels) or _env_from_name(name)
        props = {
            "location": b.get("location", ""),
            "storage_class": b.get("storageClass", ""),
            "created": b.get("timeCreated", ""),
            "versioning": (b.get("versioning") or {}).get("enabled", False),
            "labels": labels,
        }
        _insert_resource(conn, "storage", name, env, props,
                         project, b.get("location", "") or "global",
                         "gcp:storage:Bucket")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_sql(conn, stats, *, project, region, gcloud_cli) -> int:
    """Cloud SQL instances."""
    instances = _gcloud_json(gcloud_cli, project, ["sql", "instances", "list"])
    count = 0
    for inst in instances:
        name = inst.get("name", "")
        if not name:
            continue
        env = _env_from_name(name)
        ip = ""
        for addr in inst.get("ipAddresses") or []:
            if addr.get("type") == "PRIMARY":
                ip = addr.get("ipAddress", "")
                break
        props = {
            "database_version": inst.get("databaseVersion", ""),
            "tier": (inst.get("settings") or {}).get("tier", ""),
            "state": inst.get("state", ""),
            "region": inst.get("region", ""),
            "endpoint": ip,
            "connection_name": inst.get("connectionName", ""),
        }
        _insert_resource(conn, "sql", name, env, props,
                         project, inst.get("region", "") or "global",
                         "gcp:sqladmin:Instance")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_gke(conn, stats, *, project, region, gcloud_cli) -> int:
    """GKE clusters + node pools."""
    clusters = _gcloud_json(gcloud_cli, project,
                            ["container", "clusters", "list"])
    count = 0
    for c in clusters:
        name = c.get("name", "")
        if not name:
            continue
        labels = c.get("resourceLabels") or {}
        env = _env_from_labels(labels) or _env_from_name(name)
        loc = c.get("location", "")
        props = {
            "current_node_count": c.get("currentNodeCount", 0),
            "status": c.get("status", ""),
            "endpoint": c.get("endpoint", ""),
            "master_version": c.get("currentMasterVersion", ""),
            "labels": labels,
        }
        _insert_resource(conn, "gke", name, env, props,
                         project, loc or "global",
                         "gcp:container:Cluster")
        count += 1
        # Node pools.
        for pool in c.get("nodePools") or []:
            pname = pool.get("name", "")
            if not pname:
                continue
            ppath = f"{name}/{pname}"
            _insert_resource(conn, "gke", ppath, env, {
                "cluster": name,
                "machine_type": (pool.get("config") or {}).get("machineType", ""),
                "initial_node_count": pool.get("initialNodeCount", 0),
                "status": pool.get("status", ""),
            }, project, loc or "global", "gcp:container:NodePool")
            count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_functions(conn, stats, *, project, region, gcloud_cli) -> int:
    """Cloud Functions."""
    fns = _gcloud_json(gcloud_cli, project, ["functions", "list"])
    count = 0
    for f in fns:
        full = f.get("name", "")
        name = full.rsplit("/", 1)[-1] if "/" in full else full
        if not name:
            continue
        labels = f.get("labels") or {}
        env = _env_from_labels(labels) or _env_from_name(name)
        props = {
            "runtime": f.get("runtime", "") or (f.get("buildConfig") or {}).get("runtime", ""),
            "status": f.get("state", "") or f.get("status", ""),
            "url": f.get("url", "") or (f.get("serviceConfig") or {}).get("uri", ""),
            "labels": labels,
        }
        _insert_resource(conn, "functions", name, env, props,
                         project, region or "global",
                         "gcp:cloudfunctions:Function")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_run(conn, stats, *, project, region, gcloud_cli) -> int:
    """Cloud Run services."""
    services = _gcloud_json(gcloud_cli, project, ["run", "services", "list"])
    count = 0
    for s in services:
        meta = s.get("metadata") or {}
        name = meta.get("name", "") or s.get("name", "").rsplit("/", 1)[-1]
        if not name:
            continue
        labels = meta.get("labels") or s.get("labels") or {}
        env = _env_from_labels(labels) or _env_from_name(name)
        props = {
            "url": s.get("uri", "") or (s.get("status") or {}).get("url", ""),
            "region": s.get("region", "") or meta.get("namespace", ""),
            "labels": labels,
        }
        _insert_resource(conn, "run", name, env, props,
                         project, props.get("region") or "global",
                         "gcp:run:Service")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_pubsub(conn, stats, *, project, region, gcloud_cli) -> int:
    """Pub/Sub topics + subscriptions."""
    count = 0
    topics = _gcloud_json(gcloud_cli, project, ["pubsub", "topics", "list"])
    for t in topics:
        full = t.get("name", "")
        name = full.rsplit("/", 1)[-1]
        if not name:
            continue
        env = _env_from_name(name)
        props = {"arn": full, "labels": t.get("labels") or {}}
        _insert_resource(conn, "pubsub", name, env, props,
                         project, "global", "gcp:pubsub:Topic")
        count += 1

    subs = _gcloud_json(gcloud_cli, project,
                        ["pubsub", "subscriptions", "list"])
    for s in subs:
        full = s.get("name", "")
        name = full.rsplit("/", 1)[-1]
        if not name:
            continue
        env = _env_from_name(name)
        props = {
            "arn": full,
            "topic": (s.get("topic") or "").rsplit("/", 1)[-1],
            "ack_deadline_seconds": s.get("ackDeadlineSeconds", 0),
        }
        _insert_resource(conn, "pubsub", name, env, props,
                         project, "global", "gcp:pubsub:Subscription")
        count += 1

    stats.discovered += count
    stats.inserted += count
    return count


def _import_bigquery(conn, stats, *, project, region, gcloud_cli) -> int:
    """BigQuery datasets — `bq ls` via gcloud doesn't exist, use `bq` directly."""
    # gcloud has `gcloud alpha bq`, but `bq ls --format=json` is more reliable.
    try:
        result = subprocess.run(
            ["bq", "ls", "--format=json", "--project_id", project],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise _GcloudError(f"bq ls failed: {result.stderr.strip()[:200]}")
        datasets = json.loads(result.stdout or "[]")
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as exc:
        raise _GcloudError(f"bq ls: {exc}")

    count = 0
    for ds in datasets:
        ref = ds.get("datasetReference") or {}
        name = ref.get("datasetId", "")
        if not name:
            continue
        env = _env_from_name(name)
        labels = ds.get("labels") or {}
        props = {
            "location": ds.get("location", ""),
            "creation_time": ds.get("creationTime", ""),
            "labels": labels,
        }
        _insert_resource(conn, "bigquery", name, env, props,
                         project, ds.get("location", "") or "global",
                         "gcp:bigquery:Dataset")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_iam(conn, stats, *, project, region, gcloud_cli) -> int:
    """Service accounts."""
    accounts = _gcloud_json(gcloud_cli, project,
                            ["iam", "service-accounts", "list"])
    count = 0
    for a in accounts:
        name = a.get("email", "") or a.get("displayName", "")
        if not name:
            continue
        env = _env_from_name(name)
        props = {
            "unique_id": a.get("uniqueId", ""),
            "email": a.get("email", ""),
            "display_name": a.get("displayName", ""),
            "disabled": a.get("disabled", False),
        }
        _insert_resource(conn, "iam", name, env, props,
                         project, "global", "gcp:iam:ServiceAccount")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_network(conn, stats, *, project, region, gcloud_cli) -> int:
    """VPC networks + subnetworks + firewall rules."""
    count = 0
    for kind, args, gcp_kind in (
        ("Network", ["compute", "networks", "list"], "gcp:compute:Network"),
        ("Subnet", ["compute", "networks", "subnets", "list"], "gcp:compute:Subnetwork"),
        ("Firewall", ["compute", "firewall-rules", "list"], "gcp:compute:Firewall"),
    ):
        items = _gcloud_json(gcloud_cli, project, args)
        for item in items:
            name = item.get("name", "")
            if not name:
                continue
            env = _env_from_name(name)
            props = {k: v for k, v in item.items()
                     if k in ("ipCidrRange", "network", "region", "direction",
                              "priority", "sourceRanges", "targetTags",
                              "autoCreateSubnetworks", "routingConfig")}
            _insert_resource(conn, "network", name, env, props,
                             project, item.get("region", "") or "global",
                             gcp_kind)
            count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_dns(conn, stats, *, project, region, gcloud_cli) -> int:
    """Cloud DNS managed zones."""
    zones = _gcloud_json(gcloud_cli, project, ["dns", "managed-zones", "list"])
    count = 0
    for z in zones:
        name = z.get("name", "")
        if not name:
            continue
        env = _env_from_name(name)
        props = {
            "dns_name": z.get("dnsName", ""),
            "visibility": z.get("visibility", ""),
            "labels": z.get("labels") or {},
        }
        _insert_resource(conn, "dns", name, env, props,
                         project, "global", "gcp:dns:ManagedZone")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_secrets(conn, stats, *, project, region, gcloud_cli) -> int:
    """Secret Manager + Memorystore (Redis)."""
    count = 0
    try:
        secrets = _gcloud_json(gcloud_cli, project,
                               ["secrets", "list"])
        for s in secrets:
            full = s.get("name", "")
            name = full.rsplit("/", 1)[-1]
            if not name:
                continue
            env = _env_from_name(name)
            props = {
                "arn": full,
                "created": s.get("createTime", ""),
                "labels": s.get("labels") or {},
            }
            _insert_resource(conn, "secrets", name, env, props,
                             project, "global", "gcp:secretmanager:Secret")
            count += 1
    except _GcloudError:
        pass

    try:
        redis = _gcloud_json(gcloud_cli, project,
                            ["redis", "instances", "list", "--region", region or "us-central1"])
        for r in redis:
            full = r.get("name", "")
            name = full.rsplit("/", 1)[-1]
            if not name:
                continue
            env = _env_from_name(name)
            props = {
                "host": r.get("host", ""),
                "port": r.get("port", 0),
                "memory_size_gb": r.get("memorySizeGb", 0),
                "tier": r.get("tier", ""),
                "endpoint": f"{r.get('host', '')}:{r.get('port', 0)}",
            }
            _insert_resource(conn, "secrets", name, env, props,
                             project, r.get("locationId", "") or "global",
                             "gcp:redis:Instance")
            count += 1
    except _GcloudError:
        pass

    stats.discovered += count
    stats.inserted += count
    return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _GcloudError(Exception):
    pass


def _gcloud_default_project(gcloud_cli: str) -> str:
    try:
        result = subprocess.run(
            [gcloud_cli, "config", "get-value", "project"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _gcloud_json(gcloud_cli: str, project: str, args: list[str]) -> list:
    """Run a gcloud subcommand, parse JSON. Raises _GcloudError."""
    argv = [gcloud_cli] + args + [
        "--project", project, "--format=json", "--quiet",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise _GcloudError(f"timeout: {' '.join(argv)}")
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip().splitlines()
        msg = err[-1] if err else f"gcloud exited {result.returncode}"
        raise _GcloudError(f"{' '.join(args)}: {msg}")
    try:
        out = json.loads(result.stdout or "[]")
        if isinstance(out, list):
            return out
        return out.get("items") or []
    except json.JSONDecodeError as exc:
        raise _GcloudError(f"non-JSON output from {' '.join(args)}: {exc}")


def _gcp_first_internal_ip(inst: dict) -> str:
    for iface in inst.get("networkInterfaces") or []:
        if iface.get("networkIP"):
            return iface["networkIP"]
    return ""


# Same env-inference helpers as aws_import, parameterized over labels.
def _env_from_labels(labels: dict) -> str:
    for key in ("environment", "env", "stage", "tier"):
        v = str(labels.get(key, "")).strip().lower()
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


def _insert_resource(conn, service, name, env, props, project, region, kind) -> None:
    """Same shape as aws_import._insert_resource, parameterized for GCP."""
    file_path = _VIRTUAL_PATH_TEMPLATE.format(
        project=project, region=region, service=service,
    )
    now = datetime.now(timezone.utc).isoformat()
    upsert_file(conn, FileRow(
        path=file_path, project_path="",
        name=f"gcp:{service}", extension="",
        size_bytes=0, content_hash=f"gcp:{project}:{region}:{service}",
        modified_at=now, risk_tier=_tier_for_env(env),
    ))
    tier = _tier_for_env(env)
    props_with_provenance = dict(props)
    props_with_provenance["discovered_from"] = "gcloud-cli"
    props_with_provenance["project"] = project
    props_with_provenance["region"] = region
    uid = resource_uid(kind, name, region, file_path)
    upsert_resource(conn, ResourceRow(
        uid=uid, file_path=file_path, kind=kind,
        name=name, namespace=region, environment=env, risk_tier=tier,
        properties=props_with_provenance,
        context_snippet=f"discovered in {project}/{region} via gcloud-cli",
    ))
    upsert_edge(conn, EdgeSpec(
        src_kind="resource", src_id=uid,
        dst_kind="technology", dst_id="gcp",
        rel_type="USES",
    ))
