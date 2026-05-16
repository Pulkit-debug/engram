"""Helm chart extractor.

A Helm chart is a directory: Chart.yaml + values.yaml + templates/*.
We treat Chart.yaml as the entry point and emit one helm:chart Resource.
Templates are processed by the YAMLExtractor when crawled directly; we
do NOT attempt Go-template evaluation here (too lossy for static analysis).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from engram.extractors.base import (
    BaseExtractor,
    ExtractedEdge,
    ExtractedResource,
    ExtractionResult,
)


class HelmChartExtractor(BaseExtractor):
    """Triggered on Chart.yaml only."""

    def extract(self, path: Path, content: str) -> ExtractionResult:
        result = ExtractionResult(file_path=str(path))
        result.technologies.append("helm")
        try:
            doc = yaml.safe_load(content) or {}
        except yaml.YAMLError:
            return result
        if not isinstance(doc, dict):
            return result

        name = str(doc.get("name") or path.parent.name)
        chart_version = str(doc.get("version") or "")
        app_version = str(doc.get("appVersion") or "")
        chart_type = str(doc.get("type") or "application")

        props: dict[str, Any] = {
            "version": chart_version,
            "appVersion": app_version,
            "type": chart_type,
            "apiVersion": doc.get("apiVersion"),
            "description": doc.get("description"),
        }
        result.resources.append(ExtractedResource(
            name=name, kind="helm:chart",
            properties=props,
            context_snippet=f"Helm chart {name} v{chart_version}",
        ))

        # dependencies: declared chart deps -> DEPENDS_ON edges.
        deps = doc.get("dependencies") or []
        if isinstance(deps, list):
            for d in deps:
                if not isinstance(d, dict):
                    continue
                dep_name = str(d.get("name") or "")
                if not dep_name:
                    continue
                result.edges.append(ExtractedEdge(
                    source_name=name, source_kind="resource",
                    target_name=dep_name, target_kind="resource",
                    rel_type="DEPENDS_ON",
                    properties={
                        "version": d.get("version", ""),
                        "repository": d.get("repository", ""),
                        "condition": d.get("condition", ""),
                    },
                ))
                if isinstance(d.get("repository"), str):
                    result.technologies.append(d["repository"])

        return result


class HelmValuesExtractor(BaseExtractor):
    """Triggered on values.yaml / values-*.yaml.

    We harvest env-var-like keys (for cross-reference with K8s env refs) and
    a values:document resource for visibility.
    """

    def extract(self, path: Path, content: str) -> ExtractionResult:
        result = ExtractionResult(file_path=str(path))
        try:
            doc = yaml.safe_load(content) or {}
        except yaml.YAMLError:
            return result
        if not isinstance(doc, dict):
            return result

        # Chart name = parent directory name (Helm convention).
        chart_name = path.parent.name
        result.resources.append(ExtractedResource(
            name=f"{chart_name}/{path.stem}",
            kind="helm:values",
            properties={"top_level_keys": sorted(doc.keys())[:20]},
            context_snippet=f"Helm values at {path}",
        ))
        return result
