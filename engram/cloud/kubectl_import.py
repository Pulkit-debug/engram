"""Live Kubernetes cluster discovery via kubectl.

Same pattern as aws_import: Engram never calls the Kubernetes API. The user's
`kubectl` does (with their existing kubeconfig). Engram parses the JSON.

  engram import-cluster                       # current context, all namespaces
  engram import-cluster --context prod        # specific kubeconfig context
  engram import-cluster --namespace default   # one namespace only

The discovered Resources are keyed by `(kind, name, namespace)`, same as
the YAML extractor's K8s output — so a Resource discovered live in a cluster
and the same Resource defined in a YAML manifest both produce the same
`(kind, name, namespace)` tuple. The graph naturally joins them.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from engram.graph import (
    EdgeSpec, FileRow, ResourceRow,
    resource_uid, upsert_edge, upsert_file, upsert_resource,
    upsert_technology, RISK_GREEN, RISK_ORANGE, RISK_RED,
)

logger = logging.getLogger(__name__)


_VIRTUAL_PATH_TEMPLATE = "kube://{context}/{namespace}"


_DEFAULT_KINDS = (
    "Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Pod",
    "Service", "ConfigMap", "Secret", "Ingress",
    "Job", "CronJob",
    "ServiceAccount", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding",
    "PersistentVolume", "PersistentVolumeClaim", "StorageClass",
    "HorizontalPodAutoscaler", "NetworkPolicy",
)


@dataclass
class ClusterImportStats:
    discovered: int = 0
    inserted: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    per_kind: dict[str, int] = field(default_factory=dict)


def import_cluster(
    conn: sqlite3.Connection,
    *,
    context: str | None = None,
    namespace: str | None = None,
    kinds: list[str] | None = None,
    kubectl: str = "kubectl",
) -> ClusterImportStats:
    """Import the current state of a live K8s cluster into the graph.

    Args:
        conn:      sqlite3 connection
        context:   kubeconfig context name (None = current)
        namespace: single namespace or None for all-namespaces
        kinds:     which Kubernetes kinds to fetch (default: common workload + RBAC types)
        kubectl:   path to kubectl CLI
    """
    stats = ClusterImportStats()
    if not shutil.which(kubectl):
        stats.errors.append(("__cli__", f"kubectl not found at '{kubectl}'."))
        return stats

    actual_context = context or _current_context(kubectl)
    kinds = list(kinds or _DEFAULT_KINDS)

    for kind in kinds:
        try:
            n = _import_kind(conn, stats, kind, actual_context, namespace, kubectl)
            stats.per_kind[kind] = n
        except _KubectlError as exc:
            stats.errors.append((kind, str(exc)))

    upsert_technology(conn, "kubernetes", "tool")
    return stats


# ---------------------------------------------------------------------------
# Per-kind importer
# ---------------------------------------------------------------------------

def _import_kind(
    conn: sqlite3.Connection,
    stats: ClusterImportStats,
    kind: str,
    context: str,
    namespace: str | None,
    kubectl: str,
) -> int:
    out = _kubectl_json(kubectl, kind, context, namespace)
    items = out.get("items") or []
    count = 0
    now = datetime.now(timezone.utc).isoformat()

    for item in items:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        name = meta.get("name", "")
        ns = meta.get("namespace", "") or namespace or "default"
        if not name:
            continue

        labels = meta.get("labels") or {}
        env = _env_from_labels(labels) or _env_from_namespace(ns)

        # Trim the object before storing: keep metadata + spec.replicas / spec.image
        # but drop status (huge and ephemeral).
        spec = item.get("spec") or {}
        trimmed_props: dict[str, Any] = {
            "discovered_from": "kubectl",
            "context": context,
            "labels": labels,
            "annotations": meta.get("annotations") or {},
            "creation_timestamp": meta.get("creationTimestamp", ""),
        }
        if isinstance(spec, dict):
            for k in ("replicas", "selector", "strategy", "type", "clusterIP",
                      "storageClassName", "serviceAccountName"):
                if k in spec:
                    trimmed_props[k] = spec[k]
            # First container image, if present (for workloads).
            images = _container_images(spec)
            if images:
                trimmed_props["images"] = images

        file_path = _VIRTUAL_PATH_TEMPLATE.format(context=context, namespace=ns)
        upsert_file(conn, FileRow(
            path=file_path, project_path="",
            name=f"cluster:{context}/{ns}", extension="",
            size_bytes=0, content_hash=f"kube:{context}:{ns}",
            modified_at=now,
            risk_tier=_tier_for_env(env),
        ))

        resource_kind = f"k8s:{kind}"
        uid = resource_uid(resource_kind, name, ns, file_path)
        upsert_resource(conn, ResourceRow(
            uid=uid, file_path=file_path, kind=resource_kind,
            name=name, namespace=ns, environment=env,
            risk_tier=_tier_for_env(env),
            properties=trimmed_props,
            context_snippet=f"live in cluster '{context}/{ns}'",
        ))

        # Image dependencies → technology edges.
        for img in trimmed_props.get("images", []):
            tech = img.split(":")[0].split("@")[0].split("/")[-1]
            if tech:
                upsert_technology(conn, tech)
                upsert_edge(conn, EdgeSpec(
                    src_kind="resource", src_id=uid,
                    dst_kind="technology", dst_id=tech,
                    rel_type="USES",
                    properties={"image_ref": img},
                ))

        count += 1

    stats.discovered += count
    stats.inserted += count
    return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _KubectlError(Exception):
    pass


def _current_context(kubectl: str) -> str:
    try:
        result = subprocess.run(
            [kubectl, "config", "current-context"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() or "default"
    except Exception:
        return "default"


def _kubectl_json(kubectl: str, kind: str, context: str,
                  namespace: str | None) -> dict:
    argv = [kubectl, "get", kind, "-o", "json"]
    if context:
        argv += ["--context", context]
    if namespace:
        argv += ["-n", namespace]
    else:
        argv += ["--all-namespaces"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise _KubectlError(f"timeout: {' '.join(argv)}")
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip().splitlines()
        msg = err[-1] if err else f"kubectl exited {result.returncode}"
        raise _KubectlError(f"get {kind}: {msg}")
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise _KubectlError(f"non-JSON from get {kind}: {exc}")


_PROD_NAMESPACES = frozenset({"prod", "production", "live"})
_STAGING_NAMESPACES = frozenset({"stag", "staging", "preprod", "uat"})


def _env_from_labels(labels: dict[str, Any]) -> str:
    for key in ("environment", "env", "stage", "tier",
                "app.kubernetes.io/environment"):
        v = str(labels.get(key, "")).strip().lower()
        if v in _PROD_NAMESPACES:
            return "production"
        if v in _STAGING_NAMESPACES:
            return "staging"
        if v in ("dev", "develop", "development", "test", "sandbox"):
            return "dev"
        if v:
            return v
    return ""


def _env_from_namespace(ns: str) -> str:
    n = ns.lower()
    if n in _PROD_NAMESPACES:
        return "production"
    if n in _STAGING_NAMESPACES:
        return "staging"
    if n in ("dev", "develop", "development", "test", "sandbox"):
        return "dev"
    return ""


def _tier_for_env(env: str) -> str:
    if env == "production":
        return RISK_RED
    if env == "staging":
        return RISK_ORANGE
    return RISK_GREEN


def _container_images(spec: Any) -> list[str]:
    """Walk a workload spec for container images."""
    out: list[str] = []
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("containers", "initContainers"):
                v = node.get(key)
                if isinstance(v, list):
                    for c in v:
                        if isinstance(c, dict) and isinstance(c.get("image"), str):
                            out.append(c["image"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(spec)
    return out
