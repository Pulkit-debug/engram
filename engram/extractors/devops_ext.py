"""Smaller DevOps extractors: Jenkinsfile, Makefile, shell, .env.

All deterministic, regex/line-based. No tree-sitter needed for these formats
in v0.2 — the structure is simple enough that handwritten parsers are correct
and 50x faster than tree-sitter on first run.
"""

from __future__ import annotations

import re
from pathlib import Path

from engram.extractors.base import (
    BaseExtractor,
    ExtractedEdge,
    ExtractedEntity,
    ExtractedResource,
    ExtractionResult,
)


# ---------------------------------------------------------------------------
# Jenkinsfile
# ---------------------------------------------------------------------------

_STAGE_RE = re.compile(r'\bstage\s*\(\s*["\']([^"\']+)["\']\s*\)\s*\{')
_AGENT_IMAGE_RE = re.compile(r'\bdocker\s*\{\s*image\s+["\']([^"\']+)["\']')
_ENV_BLOCK_RE = re.compile(r'\benvironment\s*\{([^}]+)\}', re.DOTALL)
# Either   KEY = 'quoted value'   or   KEY = bareword
# (the bareword form is what Jenkins uses for `credentials('x')` calls).
_ENV_PAIR_RE = re.compile(
    r'([A-Z_][A-Z0-9_]*)\s*=\s*'
    r"(?:'([^']*)'"
    r'|"([^"]*)"'
    r'|([^\s,}]+))'
)


class JenkinsfileExtractor(BaseExtractor):
    def extract(self, path: Path, content: str) -> ExtractionResult:
        result = ExtractionResult(file_path=str(path))
        result.technologies.append("jenkins")

        # Pipeline resource itself.
        pipeline_name = path.parent.name if path.name.lower().startswith("jenkinsfile") else path.stem
        result.resources.append(ExtractedResource(
            name=pipeline_name, kind="jenkins:pipeline",
            context_snippet=f"Jenkinsfile at {path}",
        ))

        # Stages.
        for m in _STAGE_RE.finditer(content):
            stage_name = m.group(1)
            result.resources.append(ExtractedResource(
                name=stage_name, kind="jenkins:stage",
                context_snippet=f"stage('{stage_name}') in {path.name}",
            ))
            result.edges.append(ExtractedEdge(
                source_name=stage_name, source_kind="resource",
                target_name=pipeline_name, target_kind="resource",
                rel_type="BELONGS_TO",
            ))

        # Docker agent images.
        for m in _AGENT_IMAGE_RE.finditer(content):
            img = m.group(1)
            tech = img.split(":")[0].split("@")[0].split("/")[-1]
            if tech:
                result.technologies.append(tech)
                result.edges.append(ExtractedEdge(
                    source_name=pipeline_name, source_kind="resource",
                    target_name=tech, target_kind="technology",
                    rel_type="USES",
                    properties={"image_ref": img},
                ))

        # Environment block.
        for m in _ENV_BLOCK_RE.finditer(content):
            block = m.group(1)
            for kv in _ENV_PAIR_RE.finditer(block):
                key = kv.group(1)
                val = (kv.group(2) or kv.group(3) or kv.group(4) or "").strip()
                result.entities.append(ExtractedEntity(
                    name=key, entity_type="env_var", value=val,
                    context_snippet=f"environment block in {path.name}",
                ))

        return result


# ---------------------------------------------------------------------------
# Makefile
# ---------------------------------------------------------------------------

_TARGET_RE = re.compile(r'^([A-Za-z_][\w./-]*)\s*:(?!=)', re.MULTILINE)
_MAKE_VAR_RE = re.compile(r'^([A-Z_][A-Z0-9_]*)\s*[:?]?=\s*(.+)$', re.MULTILINE)


class MakefileExtractor(BaseExtractor):
    def extract(self, path: Path, content: str) -> ExtractionResult:
        result = ExtractionResult(file_path=str(path))
        result.technologies.append("make")

        result.resources.append(ExtractedResource(
            name=path.parent.name, kind="make:file",
            context_snippet=f"Makefile at {path}",
        ))

        for m in _TARGET_RE.finditer(content):
            target = m.group(1).strip()
            # Skip pattern rules and special targets.
            if target.startswith(".") or "%" in target:
                continue
            result.resources.append(ExtractedResource(
                name=target, kind="make:target",
                context_snippet=f"target '{target}' in {path.name}",
            ))

        for m in _MAKE_VAR_RE.finditer(content):
            key = m.group(1)
            val = m.group(2).strip()
            result.entities.append(ExtractedEntity(
                name=key, entity_type="make_var",
                value=val[:200],
                context_snippet=f"Makefile var in {path.name}",
            ))

        return result


# ---------------------------------------------------------------------------
# Shell scripts
# ---------------------------------------------------------------------------

_SOURCE_RE = re.compile(r'^\s*(?:source|\.)\s+([^\s#]+)', re.MULTILINE)
_EXPORT_RE = re.compile(r'^\s*export\s+([A-Z_][A-Z0-9_]*)(?:\s*=\s*(.+))?', re.MULTILINE)
_VAR_REF_RE = re.compile(r'\$\{?([A-Z_][A-Z0-9_]*)\}?')


class ShellExtractor(BaseExtractor):
    def extract(self, path: Path, content: str) -> ExtractionResult:
        result = ExtractionResult(file_path=str(path))
        result.technologies.append("shell")

        # The script as a resource.
        result.resources.append(ExtractedResource(
            name=path.stem, kind="shell:script",
            context_snippet=f"shell script at {path}",
        ))

        # Exports.
        exported: set[str] = set()
        for m in _EXPORT_RE.finditer(content):
            key = m.group(1)
            val = (m.group(2) or "").strip().strip('"').strip("'")
            exported.add(key)
            result.entities.append(ExtractedEntity(
                name=key, entity_type="env_var", value=val,
                context_snippet=f"exported in {path.name}",
            ))

        # Variable references (env_ref).
        for m in _VAR_REF_RE.finditer(content):
            ref = m.group(1)
            if ref in exported:
                continue
            result.entities.append(ExtractedEntity(
                name=ref, entity_type="env_ref",
                context_snippet=f"referenced in {path.name}",
            ))

        # Sourced files.
        for m in _SOURCE_RE.finditer(content):
            target = m.group(1).strip()
            result.edges.append(ExtractedEdge(
                source_name=path.stem, source_kind="resource",
                target_name=target, target_kind="file",
                rel_type="SOURCES",
            ))

        return result


# ---------------------------------------------------------------------------
# .env files
# ---------------------------------------------------------------------------

_ENV_LINE_RE = re.compile(r'^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$', re.MULTILINE)


class EnvFileExtractor(BaseExtractor):
    def extract(self, path: Path, content: str) -> ExtractionResult:
        result = ExtractionResult(file_path=str(path))

        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = _ENV_LINE_RE.match(stripped)
            if not m:
                continue
            key = m.group(1)
            val = m.group(2).strip().strip('"').strip("'")
            result.entities.append(ExtractedEntity(
                name=key, entity_type="env_var", value=val,
                context_snippet=f"defined in {path.name}",
            ))

        # The env file itself as a resource (so it appears in service maps).
        result.resources.append(ExtractedResource(
            name=path.name, kind="env:file",
            context_snippet=f"env file at {path}",
        ))
        return result
