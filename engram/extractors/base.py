"""Extractor interface and shared data structures.

Two output kinds:
  * ExtractedResource — an infra structural unit (k8s Deployment, tf resource,
    docker-compose service, helm chart). Has a kind, name, environment, and
    arbitrary properties.
  * ExtractedEntity   — a code-level unit (function, class, env_var, package).
    Kept distinct so that risk-tier/blast-radius logic only walks Resources.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ExtractedResource:
    """A structural unit of infrastructure."""

    name: str
    kind: str                                # e.g. "k8s:Deployment", "tf:aws_rds_instance"
    namespace: str = ""
    environment: str = ""                    # 'production'/'staging'/'dev'/'' (inferred or explicit)
    properties: dict[str, Any] = field(default_factory=dict)
    context_snippet: str = ""


@dataclass
class ExtractedEntity:
    """A code-level entity (function, env_var, import, etc.)."""

    name: str
    entity_type: str                         # function, class, env_var, env_ref, package, ...
    value: str = ""
    context_snippet: str = ""


@dataclass
class ExtractedEdge:
    """An extractor-declared relationship. Source/destination are by NAME;
    the crawler resolves them to UIDs after all extractions are done."""

    source_name: str
    source_kind: str                         # 'resource' | 'entity' | 'file'
    target_name: str
    target_kind: str
    rel_type: str                            # DEPENDS_ON, EXPOSES_PORT, MOUNTS, USES_ENV, IMPORTS, ...
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractionResult:
    """Full output of one extractor on one file."""

    file_path: str
    resources: list[ExtractedResource] = field(default_factory=list)
    entities: list[ExtractedEntity] = field(default_factory=list)
    edges: list[ExtractedEdge] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)
    # Optional embeddable text content for files with no obvious linear body.
    text_content: str | None = None
    # Environment hint for the FILE itself (e.g. path contains '/prod/').
    environment_hint: str = ""


class BaseExtractor(ABC):
    """Interface every file-type extractor implements."""

    @abstractmethod
    def extract(self, path: Path, content: str) -> ExtractionResult:
        """Extract resources, entities, edges, technologies from one file."""
        ...


# ---------------------------------------------------------------------------
# Registry. Lazy imports so missing optional deps don't break the whole module.
# ---------------------------------------------------------------------------

_CACHE: dict[str, BaseExtractor] = {}


def _make(cls: type[BaseExtractor]) -> BaseExtractor:
    key = cls.__name__
    if key not in _CACHE:
        _CACHE[key] = cls()
    return _CACHE[key]


def get_extractor(path: Path) -> BaseExtractor | None:
    """Pick the right extractor for a file. Returns None if no match."""
    from engram.extractors.compose_ext import DockerComposeExtractor
    from engram.extractors.devops_ext import (
        EnvFileExtractor, JenkinsfileExtractor, MakefileExtractor, ShellExtractor,
    )
    from engram.extractors.dockerfile import DockerfileExtractor
    from engram.extractors.helm_ext import HelmChartExtractor, HelmValuesExtractor
    from engram.extractors.js_ts_ext import JsTsExtractor
    from engram.extractors.python_ext import PythonExtractor
    from engram.extractors.terraform_ext import TerraformExtractor
    from engram.extractors.yaml_ext import YAMLExtractor

    name = path.name.lower()
    ext = path.suffix.lower()

    # Exact-name infra files.
    NAME_MAP: dict[str, type[BaseExtractor]] = {
        "dockerfile": DockerfileExtractor,
        "docker-compose.yml": DockerComposeExtractor,
        "docker-compose.yaml": DockerComposeExtractor,
        "compose.yml": DockerComposeExtractor,
        "compose.yaml": DockerComposeExtractor,
        "jenkinsfile": JenkinsfileExtractor,
        "makefile": MakefileExtractor,
        "gnumakefile": MakefileExtractor,
        "chart.yaml": HelmChartExtractor,
        "chart.yml": HelmChartExtractor,
        "values.yaml": HelmValuesExtractor,
        "values.yml": HelmValuesExtractor,
    }
    if name in NAME_MAP:
        return _make(NAME_MAP[name])

    # Helm values variants like values-prod.yaml, values.staging.yml.
    if (name.startswith("values.") or name.startswith("values-")) and ext in (".yaml", ".yml"):
        return _make(HelmValuesExtractor)

    # .env and variants (.env, .env.local, .env.production).
    if name == ".env" or name.startswith(".env"):
        return _make(EnvFileExtractor)

    # Prefix match for variants.
    for prefix, cls in (
        ("dockerfile", DockerfileExtractor),
        ("docker-compose", DockerComposeExtractor),
        ("compose", DockerComposeExtractor),
        ("jenkinsfile", JenkinsfileExtractor),
        ("makefile", MakefileExtractor),
    ):
        if name.startswith(prefix):
            return _make(cls)

    # Extension match.
    EXT_MAP: dict[str, type[BaseExtractor]] = {
        ".py": PythonExtractor,
        ".tf": TerraformExtractor,
        ".tfvars": TerraformExtractor,
        ".hcl": TerraformExtractor,
        ".yaml": YAMLExtractor,
        ".yml": YAMLExtractor,
        ".js": JsTsExtractor,
        ".jsx": JsTsExtractor,
        ".ts": JsTsExtractor,
        ".tsx": JsTsExtractor,
        ".mjs": JsTsExtractor,
        ".cjs": JsTsExtractor,
        ".sh": ShellExtractor,
        ".bash": ShellExtractor,
        ".zsh": ShellExtractor,
    }
    if ext in EXT_MAP:
        return _make(EXT_MAP[ext])

    return None
