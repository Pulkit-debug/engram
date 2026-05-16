"""Azure resource discovery via the az CLI.

Same shell-out pattern as aws_import / gcp_import. The user's `az login` is
used; Engram never authenticates to Azure.

Supported kinds (v0.4):
  rg          — Resource Groups (Azure's primary grouping primitive)
  vm          — Virtual Machines + Disks
  storage     — Storage Accounts
  sql         — SQL Databases
  cosmosdb    — Cosmos DB accounts
  aks         — AKS clusters
  functions   — Azure Functions
  servicebus  — Service Bus namespaces
  eventhubs   — Event Hubs namespaces
  appservice  — App Service + Container Apps
  network     — VNets + NSGs + Private DNS Zones
  keyvault    — Key Vault
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

_VIRTUAL_PATH_TEMPLATE = "azure://{sub}/{region}/{service}"


@dataclass
class CloudImportStats:
    discovered: int = 0
    inserted: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    per_kind: dict[str, int] = field(default_factory=dict)


def import_azure(
    conn,
    *,
    kinds: list[str],
    subscription: str | None = None,
    az_cli: str = "az",
) -> CloudImportStats:
    """Discover Azure resources and insert into the graph."""
    stats = CloudImportStats()

    if not shutil.which(az_cli):
        stats.errors.append(("__cli__", f"az CLI not found at '{az_cli}'. "
                             "Install: https://learn.microsoft.com/cli/azure/install-azure-cli"))
        return stats

    sub_id = subscription or _az_default_subscription(az_cli)
    if not sub_id:
        stats.errors.append(("__cli__",
            "No Azure subscription set. Run `az login` and `az account set --subscription <id>`."))
        return stats

    importers: dict[str, Callable] = {
        "rg":         _import_resource_groups,
        "vm":         _import_vms,
        "storage":    _import_storage_accounts,
        "sql":        _import_sql,
        "cosmosdb":   _import_cosmos,
        "aks":        _import_aks,
        "functions":  _import_functions,
        "servicebus": _import_servicebus,
        "eventhubs":  _import_eventhubs,
        "appservice": _import_appservice,
        "network":    _import_network,
        "keyvault":   _import_keyvault,
    }

    for kind in kinds:
        if kind not in importers:
            stats.errors.append((kind, f"unknown kind. Supported: {sorted(importers)}"))
            continue
        try:
            n = importers[kind](conn, stats, subscription=sub_id, az_cli=az_cli)
            stats.per_kind[kind] = n
        except _AzError as exc:
            stats.errors.append((kind, str(exc)))

    upsert_technology(conn, "azure", "cloud")

    try:
        from engram.inference.cloud_attachments import link_cloud_attachments
        link_cloud_attachments(conn)
    except Exception as exc:
        logger.warning("cloud-attachment linking (azure) failed: %s", exc)

    return stats


# ---------------------------------------------------------------------------
# Per-service
# ---------------------------------------------------------------------------

def _import_resource_groups(conn, stats, *, subscription, az_cli) -> int:
    """Azure Resource Groups."""
    rgs = _az_json(az_cli, subscription, ["group", "list"])
    count = 0
    for rg in rgs:
        name = rg.get("name", "")
        if not name:
            continue
        tags = rg.get("tags") or {}
        env = _env_from_tags(tags) or _env_from_name(name)
        props = {
            "id": rg.get("id", ""),
            "location": rg.get("location", ""),
            "provisioning_state": (rg.get("properties") or {}).get("provisioningState", ""),
            "tags": tags,
        }
        _insert_resource(conn, "rg", name, env, props,
                         subscription, rg.get("location", "") or "global",
                         "azure:resources:ResourceGroup")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_vms(conn, stats, *, subscription, az_cli) -> int:
    """VMs + Disks."""
    count = 0
    vms = _az_json(az_cli, subscription, ["vm", "list"])
    for vm in vms:
        name = vm.get("name", "")
        if not name:
            continue
        tags = vm.get("tags") or {}
        env = _env_from_tags(tags) or _env_from_name(name)
        props = {
            "id": vm.get("id", ""),
            "vm_size": (vm.get("hardwareProfile") or {}).get("vmSize", ""),
            "location": vm.get("location", ""),
            "os_type": (vm.get("storageProfile") or {}).get("osDisk", {}).get("osType", ""),
            "tags": tags,
        }
        _insert_resource(conn, "vm", name, env, props,
                         subscription, vm.get("location", "") or "global",
                         "azure:compute:VirtualMachine")
        count += 1

    disks = _az_json(az_cli, subscription, ["disk", "list"])
    for d in disks:
        name = d.get("name", "")
        if not name:
            continue
        tags = d.get("tags") or {}
        env = _env_from_tags(tags) or _env_from_name(name)
        props = {
            "id": d.get("id", ""),
            "disk_size_gb": d.get("diskSizeGb", 0),
            "sku": (d.get("sku") or {}).get("name", ""),
            "location": d.get("location", ""),
            "tags": tags,
        }
        _insert_resource(conn, "vm", name, env, props,
                         subscription, d.get("location", "") or "global",
                         "azure:compute:Disk")
        count += 1

    stats.discovered += count
    stats.inserted += count
    return count


def _import_storage_accounts(conn, stats, *, subscription, az_cli) -> int:
    accounts = _az_json(az_cli, subscription, ["storage", "account", "list"])
    count = 0
    for a in accounts:
        name = a.get("name", "")
        if not name:
            continue
        tags = a.get("tags") or {}
        env = _env_from_tags(tags) or _env_from_name(name)
        endpoints = a.get("primaryEndpoints") or {}
        props = {
            "id": a.get("id", ""),
            "kind": a.get("kind", ""),
            "sku": (a.get("sku") or {}).get("name", ""),
            "location": a.get("location", ""),
            "endpoint": endpoints.get("blob", ""),
            "tags": tags,
        }
        _insert_resource(conn, "storage", name, env, props,
                         subscription, a.get("location", "") or "global",
                         "azure:storage:Account")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_sql(conn, stats, *, subscription, az_cli) -> int:
    """SQL Servers + Databases."""
    count = 0
    servers = _az_json(az_cli, subscription, ["sql", "server", "list"])
    for s in servers:
        name = s.get("name", "")
        if not name:
            continue
        env = _env_from_name(name)
        props = {
            "id": s.get("id", ""),
            "fully_qualified_domain_name": s.get("fullyQualifiedDomainName", ""),
            "location": s.get("location", ""),
            "version": s.get("version", ""),
        }
        _insert_resource(conn, "sql", name, env, props,
                         subscription, s.get("location", "") or "global",
                         "azure:sql:Server")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_cosmos(conn, stats, *, subscription, az_cli) -> int:
    accounts = _az_json(az_cli, subscription, ["cosmosdb", "list"])
    count = 0
    for a in accounts:
        name = a.get("name", "")
        if not name:
            continue
        tags = a.get("tags") or {}
        env = _env_from_tags(tags) or _env_from_name(name)
        props = {
            "id": a.get("id", ""),
            "endpoint": a.get("documentEndpoint", ""),
            "kind": a.get("kind", ""),
            "location": a.get("location", ""),
            "tags": tags,
        }
        _insert_resource(conn, "cosmosdb", name, env, props,
                         subscription, a.get("location", "") or "global",
                         "azure:cosmosdb:DatabaseAccount")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_aks(conn, stats, *, subscription, az_cli) -> int:
    clusters = _az_json(az_cli, subscription, ["aks", "list"])
    count = 0
    for c in clusters:
        name = c.get("name", "")
        if not name:
            continue
        tags = c.get("tags") or {}
        env = _env_from_tags(tags) or _env_from_name(name)
        props = {
            "id": c.get("id", ""),
            "kubernetes_version": c.get("kubernetesVersion", ""),
            "location": c.get("location", ""),
            "fqdn": c.get("fqdn", ""),
            "node_pool_count": len(c.get("agentPoolProfiles") or []),
            "tags": tags,
        }
        _insert_resource(conn, "aks", name, env, props,
                         subscription, c.get("location", "") or "global",
                         "azure:containerservice:ManagedCluster")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_functions(conn, stats, *, subscription, az_cli) -> int:
    """Azure Functions are surfaced as function-app web sites."""
    apps = _az_json(az_cli, subscription, ["functionapp", "list"])
    count = 0
    for a in apps:
        name = a.get("name", "")
        if not name:
            continue
        tags = a.get("tags") or {}
        env = _env_from_tags(tags) or _env_from_name(name)
        props = {
            "id": a.get("id", ""),
            "default_host_name": a.get("defaultHostName", ""),
            "state": a.get("state", ""),
            "kind": a.get("kind", ""),
            "tags": tags,
        }
        _insert_resource(conn, "functions", name, env, props,
                         subscription, a.get("location", "") or "global",
                         "azure:web:FunctionApp")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_servicebus(conn, stats, *, subscription, az_cli) -> int:
    nses = _az_json(az_cli, subscription, ["servicebus", "namespace", "list"])
    count = 0
    for ns in nses:
        name = ns.get("name", "")
        if not name:
            continue
        tags = ns.get("tags") or {}
        env = _env_from_tags(tags) or _env_from_name(name)
        props = {
            "id": ns.get("id", ""),
            "endpoint": ns.get("serviceBusEndpoint", ""),
            "sku": (ns.get("sku") or {}).get("name", ""),
            "tags": tags,
        }
        _insert_resource(conn, "servicebus", name, env, props,
                         subscription, ns.get("location", "") or "global",
                         "azure:servicebus:Namespace")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_eventhubs(conn, stats, *, subscription, az_cli) -> int:
    nses = _az_json(az_cli, subscription, ["eventhubs", "namespace", "list"])
    count = 0
    for ns in nses:
        name = ns.get("name", "")
        if not name:
            continue
        tags = ns.get("tags") or {}
        env = _env_from_tags(tags) or _env_from_name(name)
        props = {
            "id": ns.get("id", ""),
            "endpoint": ns.get("serviceBusEndpoint", ""),
            "sku": (ns.get("sku") or {}).get("name", ""),
            "tags": tags,
        }
        _insert_resource(conn, "eventhubs", name, env, props,
                         subscription, ns.get("location", "") or "global",
                         "azure:eventhub:Namespace")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_appservice(conn, stats, *, subscription, az_cli) -> int:
    """App Service web apps + Container Apps."""
    count = 0
    web = _az_json(az_cli, subscription, ["webapp", "list"])
    for app in web:
        name = app.get("name", "")
        if not name:
            continue
        tags = app.get("tags") or {}
        env = _env_from_tags(tags) or _env_from_name(name)
        props = {
            "id": app.get("id", ""),
            "default_host_name": app.get("defaultHostName", ""),
            "state": app.get("state", ""),
            "tags": tags,
        }
        _insert_resource(conn, "appservice", name, env, props,
                         subscription, app.get("location", "") or "global",
                         "azure:web:Site")
        count += 1

    try:
        capps = _az_json(az_cli, subscription, ["containerapp", "list"])
        for app in capps:
            name = app.get("name", "")
            if not name:
                continue
            tags = app.get("tags") or {}
            env = _env_from_tags(tags) or _env_from_name(name)
            props = {
                "id": app.get("id", ""),
                "fqdn": ((app.get("properties") or {}).get("configuration") or {}).get("ingress", {}).get("fqdn", ""),
                "location": app.get("location", ""),
                "tags": tags,
            }
            _insert_resource(conn, "appservice", name, env, props,
                             subscription, app.get("location", "") or "global",
                             "azure:app:ContainerApp")
            count += 1
    except _AzError:
        pass

    stats.discovered += count
    stats.inserted += count
    return count


def _import_network(conn, stats, *, subscription, az_cli) -> int:
    """VNets + NSGs + Private DNS Zones."""
    count = 0
    for kind_args in (
        (["network", "vnet", "list"], "azure:network:VirtualNetwork"),
        (["network", "nsg", "list"], "azure:network:NetworkSecurityGroup"),
        (["network", "private-dns", "zone", "list"], "azure:network:PrivateDnsZone"),
    ):
        args, gcp_kind = kind_args
        try:
            items = _az_json(az_cli, subscription, args)
        except _AzError:
            continue
        for item in items:
            name = item.get("name", "")
            if not name:
                continue
            tags = item.get("tags") or {}
            env = _env_from_tags(tags) or _env_from_name(name)
            props = {
                "id": item.get("id", ""),
                "location": item.get("location", ""),
                "address_space": (item.get("addressSpace") or {}).get("addressPrefixes", []),
                "tags": tags,
            }
            _insert_resource(conn, "network", name, env, props,
                             subscription, item.get("location", "") or "global",
                             gcp_kind)
            count += 1
    stats.discovered += count
    stats.inserted += count
    return count


def _import_keyvault(conn, stats, *, subscription, az_cli) -> int:
    vaults = _az_json(az_cli, subscription, ["keyvault", "list"])
    count = 0
    for v in vaults:
        name = v.get("name", "")
        if not name:
            continue
        tags = v.get("tags") or {}
        env = _env_from_tags(tags) or _env_from_name(name)
        props = {
            "id": v.get("id", ""),
            "vault_uri": (v.get("properties") or {}).get("vaultUri", ""),
            "location": v.get("location", ""),
            "tags": tags,
        }
        _insert_resource(conn, "keyvault", name, env, props,
                         subscription, v.get("location", "") or "global",
                         "azure:keyvault:Vault")
        count += 1
    stats.discovered += count
    stats.inserted += count
    return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _AzError(Exception):
    pass


def _az_default_subscription(az_cli: str) -> str:
    try:
        result = subprocess.run(
            [az_cli, "account", "show", "--query", "id", "-o", "tsv"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _az_json(az_cli: str, subscription: str, args: list[str]) -> list:
    argv = [az_cli] + args + [
        "--subscription", subscription, "--output", "json",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise _AzError(f"timeout: {' '.join(argv)}")
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip().splitlines()
        msg = err[-1] if err else f"az exited {result.returncode}"
        raise _AzError(f"{' '.join(args)}: {msg}")
    try:
        out = json.loads(result.stdout or "[]")
        return out if isinstance(out, list) else [out]
    except json.JSONDecodeError as exc:
        raise _AzError(f"non-JSON output from {' '.join(args)}: {exc}")


def _env_from_tags(tags: dict) -> str:
    """Tag keys come in any case in Azure."""
    for key in tags:
        if key.lower() in ("environment", "env", "stage", "tier"):
            v = str(tags[key]).strip().lower()
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


def _insert_resource(conn, service, name, env, props, subscription, region, kind) -> None:
    file_path = _VIRTUAL_PATH_TEMPLATE.format(
        sub=subscription, region=region, service=service,
    )
    now = datetime.now(timezone.utc).isoformat()
    upsert_file(conn, FileRow(
        path=file_path, project_path="",
        name=f"azure:{service}", extension="",
        size_bytes=0, content_hash=f"az:{subscription}:{region}:{service}",
        modified_at=now, risk_tier=_tier_for_env(env),
    ))
    tier = _tier_for_env(env)
    props_with_prov = dict(props)
    props_with_prov["discovered_from"] = "az-cli"
    props_with_prov["subscription"] = subscription
    props_with_prov["region"] = region
    uid = resource_uid(kind, name, region, file_path)
    upsert_resource(conn, ResourceRow(
        uid=uid, file_path=file_path, kind=kind,
        name=name, namespace=region, environment=env, risk_tier=tier,
        properties=props_with_prov,
        context_snippet=f"discovered in {subscription}/{region} via az-cli",
    ))
    upsert_edge(conn, EdgeSpec(
        src_kind="resource", src_id=uid,
        dst_kind="technology", dst_id="azure",
        rel_type="USES",
    ))
