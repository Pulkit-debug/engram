"""YAML extractor with first-class Kubernetes detection.

YAML files in DevOps land are mostly:
  * Kubernetes manifests (kind+apiVersion+metadata)
  * GitHub Actions / GitLab CI / CircleCI workflows
  * Ansible playbooks
  * Generic config

We special-case Kubernetes because that's where the production-vs-staging
signal lives. Other YAMLs get a 'yaml:document' resource with shallow props.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from engram.extractors.base import (
    BaseExtractor,
    ExtractedEdge,
    ExtractedEntity,
    ExtractedResource,
    ExtractionResult,
)


_K8S_API_PREFIXES = (
    "v1", "apps/", "batch/", "rbac.authorization.k8s.io/",
    "networking.k8s.io/", "extensions/", "policy/", "scheduling.k8s.io/",
    "storage.k8s.io/", "autoscaling/", "events.k8s.io/",
    "coordination.k8s.io/", "admissionregistration.k8s.io/",
    "apiextensions.k8s.io/", "argoproj.io/", "cert-manager.io/",
    "monitoring.coreos.com/",
)

_PROD_NAMESPACES = frozenset({"prod", "production", "live"})
_STAGING_NAMESPACES = frozenset({"stage", "staging", "preprod", "uat"})


def _is_k8s(doc: Any) -> bool:
    if not isinstance(doc, dict):
        return False
    api = doc.get("apiVersion", "")
    if not isinstance(api, str) or not api:
        return False
    if "kind" not in doc or "metadata" not in doc:
        return False
    return any(api.startswith(p) for p in _K8S_API_PREFIXES) or "/" in api


def _classify_env(doc: dict, path: str) -> str:
    meta = doc.get("metadata") or {}
    ns = str(meta.get("namespace") or "").lower()
    if ns in _PROD_NAMESPACES:
        return "production"
    if ns in _STAGING_NAMESPACES:
        return "staging"
    labels = meta.get("labels") or {}
    if isinstance(labels, dict):
        for key in ("environment", "env", "stage", "tier", "engram.io/environment"):
            v = str(labels.get(key, "")).lower()
            if v in _PROD_NAMESPACES:
                return "production"
            if v in _STAGING_NAMESPACES:
                return "staging"
            if v:
                return v
    p = path.lower().replace("\\", "/")
    if any(seg in p.split("/") for seg in _PROD_NAMESPACES):
        return "production"
    if any(seg in p.split("/") for seg in _STAGING_NAMESPACES):
        return "staging"
    return ""


class YAMLExtractor(BaseExtractor):
    def extract(self, path: Path, content: str) -> ExtractionResult:
        result = ExtractionResult(file_path=str(path))
        try:
            docs = list(yaml.safe_load_all(content))
        except yaml.YAMLError:
            return result

        for doc in docs:
            if doc is None:
                continue
            if _is_k8s(doc):
                self._extract_k8s(doc, path, result)
            elif isinstance(doc, dict):
                # Generic YAML doc — emit a shallow resource so it shows up.
                # Skip if obviously not config (e.g., empty).
                if doc:
                    self._extract_generic(doc, path, result)
        return result

    # -- Kubernetes ------------------------------------------------------

    def _extract_k8s(self, doc: dict, path: Path, result: ExtractionResult) -> None:
        kind = str(doc.get("kind", "")).strip()
        meta = doc.get("metadata") or {}
        name = str(meta.get("name", "")).strip()
        namespace = str(meta.get("namespace", "")).strip()
        if not name:
            return

        env = _classify_env(doc, str(path))
        result.environment_hint = result.environment_hint or env

        # Extract spec props that matter for blast-radius.
        props: dict[str, Any] = {
            "apiVersion": doc.get("apiVersion"),
            "labels": meta.get("labels") or {},
        }
        spec = doc.get("spec") or {}
        if isinstance(spec, dict):
            for k in ("replicas", "serviceName", "selector", "strategy",
                      "storageClassName", "type", "clusterIP"):
                if k in spec:
                    props[k] = spec[k]

        result.resources.append(ExtractedResource(
            name=name,
            kind=f"k8s:{kind}",
            namespace=namespace,
            environment=env,
            properties=props,
            context_snippet=f"{kind}/{name} in {namespace or 'default'}",
        ))
        result.technologies.append("kubernetes")

        # Walk env: blocks for env_var / env_ref entities.
        for env_pair in self._walk_env_blocks(spec):
            entity = ExtractedEntity(
                name=env_pair["name"],
                entity_type="env_var" if env_pair.get("value") else "env_ref",
                value=str(env_pair.get("value", "") or env_pair.get("valueFrom", "")),
                context_snippet=f"{kind}/{name}",
            )
            result.entities.append(entity)
            result.edges.append(ExtractedEdge(
                source_name=name,
                source_kind="resource",
                target_name=env_pair["name"],
                target_kind="entity",
                rel_type="USES_ENV",
            ))

        # Walk container images.
        for img in self._walk_container_images(spec):
            tech = img.split(":")[0].split("@")[0].split("/")[-1]
            if tech:
                result.technologies.append(tech)
                result.edges.append(ExtractedEdge(
                    source_name=name, source_kind="resource",
                    target_name=tech, target_kind="technology",
                    rel_type="USES",
                    properties={"image_ref": img},
                ))

        # Walk ports.
        for port in self._walk_ports(spec):
            result.entities.append(ExtractedEntity(
                name=str(port), entity_type="port", value=str(port),
                context_snippet=f"{kind}/{name}",
            ))

        # depends_on via initContainers or wait-for hints (heuristic).
        # We skip this for v0.2 — too noisy without LLM.

    def _walk_env_blocks(self, spec: Any) -> list[dict]:
        out: list[dict] = []
        def _walk(node: Any):
            if isinstance(node, dict):
                env = node.get("env")
                if isinstance(env, list):
                    for item in env:
                        if isinstance(item, dict) and "name" in item:
                            out.append(item)
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)
        _walk(spec)
        return out

    def _walk_container_images(self, spec: Any) -> list[str]:
        out: list[str] = []
        def _walk(node: Any):
            if isinstance(node, dict):
                containers = node.get("containers") or node.get("initContainers")
                if isinstance(containers, list):
                    for c in containers:
                        if isinstance(c, dict) and isinstance(c.get("image"), str):
                            out.append(c["image"])
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)
        _walk(spec)
        return out

    def _walk_ports(self, spec: Any) -> list[int]:
        out: list[int] = []
        def _walk(node: Any):
            if isinstance(node, dict):
                ports = node.get("ports")
                if isinstance(ports, list):
                    for p in ports:
                        if isinstance(p, dict):
                            for key in ("containerPort", "port", "targetPort"):
                                if isinstance(p.get(key), int):
                                    out.append(p[key])
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)
        _walk(spec)
        return out

    # -- Generic YAML ----------------------------------------------------

    def _extract_generic(self, doc: dict, path: Path, result: ExtractionResult) -> None:
        # Detect GitHub Actions workflow.
        if "on" in doc and "jobs" in doc and isinstance(doc.get("jobs"), dict):
            self._extract_github_actions(doc, path, result)
            return

        # Detect GitLab CI.
        if any(k in doc for k in ("stages", "default", "include")) and any(
            isinstance(v, dict) and "script" in v for v in doc.values() if isinstance(v, dict)
        ):
            self._extract_gitlab_ci(doc, path, result)
            return

        # Otherwise, treat as a generic config doc.
        result.resources.append(ExtractedResource(
            name=path.stem,
            kind="yaml:document",
            properties={"top_level_keys": sorted(doc.keys())[:20]},
            context_snippet=f"YAML at {path.name}",
        ))

    def _extract_github_actions(self, doc: dict, path: Path, result: ExtractionResult) -> None:
        wf_name = str(doc.get("name") or path.stem)
        result.resources.append(ExtractedResource(
            name=wf_name,
            kind="gha:workflow",
            properties={"triggers": list(doc.get("on", {}) if isinstance(doc.get("on"), dict) else [doc.get("on")])},
            context_snippet=f"GitHub Actions workflow {wf_name}",
        ))
        result.technologies.append("github-actions")
        jobs = doc.get("jobs") or {}
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            result.resources.append(ExtractedResource(
                name=str(job_id),
                kind="gha:job",
                properties={
                    "runs_on": job.get("runs-on"),
                    "needs": job.get("needs"),
                },
                context_snippet=f"GHA job {job_id} in {wf_name}",
            ))
            result.edges.append(ExtractedEdge(
                source_name=str(job_id), source_kind="resource",
                target_name=wf_name, target_kind="resource",
                rel_type="BELONGS_TO",
            ))
            for need in (job.get("needs") or []) if isinstance(job.get("needs"), list) else []:
                result.edges.append(ExtractedEdge(
                    source_name=str(job_id), source_kind="resource",
                    target_name=str(need), target_kind="resource",
                    rel_type="DEPENDS_ON",
                ))

    def _extract_gitlab_ci(self, doc: dict, path: Path, result: ExtractionResult) -> None:
        result.resources.append(ExtractedResource(
            name=path.stem,
            kind="gitlab-ci:pipeline",
            properties={"stages": doc.get("stages", [])},
            context_snippet=f"GitLab CI {path.name}",
        ))
        result.technologies.append("gitlab-ci")
