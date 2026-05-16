"""Filesystem crawler — walks paths, runs extractors, writes the graph.

Design choices vs. v1:
  * No tier-based content reads. Either we index a file fully or we skip it.
  * Project detection picks up Helm chart and Terraform module markers too.
  * Edge-builder post-pass links env_var defs to env_ref consumers across
    files (the single most valuable cross-file relationship for DevOps).
  * Content-hash skip is fast: we hash file bytes, not file paths.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from engram.extractors.base import ExtractionResult, get_extractor
from engram.graph import (
    EdgeSpec, EntityRow, FileRow, ProjectRow, ResourceRow,
    RISK_GREEN, RISK_ORANGE, RISK_RED,
    delete_file_and_dependents, entity_uid,
    resource_uid, upsert_edge, upsert_entity, upsert_file, upsert_project,
    upsert_resource, upsert_technology,
)
from engram.safety.blast_radius import infer_environment_from_path, tier_for_environment

if TYPE_CHECKING:
    from engram.config import Config

logger = logging.getLogger(__name__)


_BINARY_EXT = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
    ".mp3", ".mp4", ".wav", ".avi", ".mov",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".exe", ".dll", ".so", ".dylib", ".o", ".a", ".class",
    ".woff", ".woff2", ".ttf", ".eot", ".pyc", ".pyo", ".whl",
    ".sqlite", ".db",
})


@dataclass
class IndexStats:
    files_scanned: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    projects_found: int = 0
    resources_extracted: int = 0
    entities_extracted: int = 0
    edges_created: int = 0
    technologies: set[str] = field(default_factory=set)
    extractor_hits: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "files_scanned": self.files_scanned,
            "files_indexed": self.files_indexed,
            "files_skipped": self.files_skipped,
            "projects_found": self.projects_found,
            "resources_extracted": self.resources_extracted,
            "entities_extracted": self.entities_extracted,
            "edges_created": self.edges_created,
            "technologies": sorted(self.technologies),
            "extractor_hits": dict(self.extractor_hits),
        }


def index_paths(
    conn: sqlite3.Connection,
    config: "Config",
    *,
    paths: list[Path] | None = None,
    force: bool = False,
) -> IndexStats:
    """Index one or more paths into the graph."""
    stats = IndexStats()
    targets = paths or config.watch_paths
    if not targets:
        logger.warning("No paths to index; configure watch_paths or pass --path.")
        return stats

    for root in targets:
        if not Path(root).exists():
            logger.warning("Path does not exist: %s", root)
            continue
        _walk_root(Path(root), conn, config, stats, force)

    # Post-pass: link env_var defs to env_ref consumers across files.
    _build_env_edges(conn, stats)

    # Post-pass: value-match inference. Links env-var/secret values to
    # cloud Resources whose endpoint/ARN/dns_name appears in the value.
    # Cheap if no cloud resources have been imported (just iterates entities).
    try:
        from engram.inference.value_match import infer_value_matches
        vm_stats = infer_value_matches(conn)
        if vm_stats.edges_inferred:
            logger.info(
                "value-match inference: %d edges inferred from %d candidates",
                vm_stats.edges_inferred, vm_stats.candidates_extracted,
            )
    except Exception as exc:
        logger.warning("value-match inference failed: %s", exc)

    return stats


def _walk_root(
    root: Path,
    conn: sqlite3.Connection,
    config: "Config",
    stats: IndexStats,
    force: bool,
) -> None:
    discovered_projects: dict[str, str] = {}

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        cur = Path(dirpath)
        # Prune dirs: ignore patterns + any dotdir.
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and not _matches_any(d, config.ignore_globs)
        ]

        # Detect project root.
        proj = _detect_project(cur, config.project_markers)
        if proj is None:
            proj = _fallback_project(cur, root)
        proj_key = str(proj) if proj else ""
        if proj and proj_key not in discovered_projects:
            ptype = _detect_project_type(proj)
            discovered_projects[proj_key] = ptype
            upsert_project(conn, ProjectRow(
                path=proj_key, name=proj.name, project_type=ptype,
            ))
            stats.projects_found += 1

        for filename in filenames:
            fp = cur / filename
            stats.files_scanned += 1
            if _should_skip(fp, config):
                stats.files_skipped += 1
                continue
            _index_file(fp, proj_key, conn, config, stats, force)


def _index_file(
    fp: Path,
    project_path: str,
    conn: sqlite3.Connection,
    config: "Config",
    stats: IndexStats,
    force: bool,
) -> None:
    try:
        stat = fp.stat()
    except OSError:
        stats.files_skipped += 1
        return

    if stat.st_size > config.max_file_size_kb * 1024:
        stats.files_skipped += 1
        return
    if stat.st_size == 0:
        stats.files_skipped += 1
        return

    extractor = get_extractor(fp)
    if extractor is None:
        stats.files_skipped += 1
        return

    try:
        raw = fp.read_bytes()
    except OSError:
        stats.files_skipped += 1
        return

    content_hash = hashlib.sha256(raw).hexdigest()[:16]
    if not force:
        existing = conn.execute(
            "SELECT content_hash FROM file WHERE path = ?", (str(fp),),
        ).fetchone()
        if existing and existing["content_hash"] == content_hash:
            stats.files_skipped += 1
            return

    # Decode for text extractors. Best-effort; ignore errors.
    try:
        content = raw.decode("utf-8", errors="ignore")
    except Exception:
        content = ""

    # Wipe prior state for this file (cascade-clears resources/entities/chunks).
    delete_file_and_dependents(conn, str(fp))

    # File-level risk_tier from path.
    env = infer_environment_from_path(str(fp))
    file_tier = tier_for_environment(env)

    modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    upsert_file(conn, FileRow(
        path=str(fp), project_path=project_path,
        name=fp.name, extension=fp.suffix,
        size_bytes=stat.st_size, content_hash=content_hash,
        modified_at=modified_at, risk_tier=file_tier,
    ))

    # Run extractor.
    try:
        extraction: ExtractionResult = extractor.extract(fp, content)
    except Exception as exc:
        logger.warning("Extractor failed on %s: %s", fp, exc)
        return

    # Resource environment: prefer extractor's hint; fall back to path inference.
    file_env_hint = extraction.environment_hint or env

    name_to_uid: dict[str, str] = {}
    for res in extraction.resources:
        res_env = res.environment or file_env_hint
        res_tier = tier_for_environment(res_env) if res_env else file_tier
        ruid = resource_uid(res.kind, res.name, res.namespace, str(fp))
        upsert_resource(conn, ResourceRow(
            uid=ruid, file_path=str(fp), kind=res.kind,
            name=res.name, namespace=res.namespace,
            environment=res_env, risk_tier=res_tier,
            properties=res.properties or {},
            context_snippet=res.context_snippet,
        ))
        name_to_uid[res.name] = ruid
        upsert_edge(conn, EdgeSpec(
            src_kind="resource", src_id=ruid,
            dst_kind="file", dst_id=str(fp),
            rel_type="DEFINED_IN",
        ))
        stats.resources_extracted += 1

    for ent in extraction.entities:
        euid = entity_uid(ent.name, ent.entity_type, str(fp))
        upsert_entity(conn, EntityRow(
            uid=euid, file_path=str(fp),
            name=ent.name, entity_type=ent.entity_type,
            value=ent.value, context_snippet=ent.context_snippet,
        ))
        stats.entities_extracted += 1
        # File CONTAINS entity
        upsert_edge(conn, EdgeSpec(
            src_kind="file", src_id=str(fp),
            dst_kind="entity", dst_id=euid,
            rel_type="CONTAINS",
        ))

    # Technologies.
    for tech in extraction.technologies:
        norm = tech.strip().lower()
        if not norm:
            continue
        upsert_technology(conn, norm)
        stats.technologies.add(norm)

    # Edges declared by the extractor — resolve names to UIDs where possible.
    for edge in extraction.edges:
        src_id = _resolve_edge_endpoint(edge.source_kind, edge.source_name, name_to_uid, str(fp))
        dst_id = _resolve_edge_endpoint(edge.target_kind, edge.target_name, name_to_uid, str(fp))
        if not src_id or not dst_id:
            continue
        upsert_edge(conn, EdgeSpec(
            src_kind=edge.source_kind, src_id=src_id,
            dst_kind=edge.target_kind, dst_id=dst_id,
            rel_type=edge.rel_type,
            properties=edge.properties or {},
        ))
        stats.edges_created += 1

    extractor_name = type(extractor).__name__
    stats.extractor_hits[extractor_name] = stats.extractor_hits.get(extractor_name, 0) + 1
    stats.files_indexed += 1


def _resolve_edge_endpoint(
    kind: str, name: str, name_to_uid: dict[str, str], file_path: str,
) -> str | None:
    """Resolve an extractor-declared endpoint name into a real id.

    Rules:
      * resource: use the local name_to_uid map; if missing, that resource
        wasn't extracted here — we use the name as a placeholder so post-pass
        can resolve it. (Edges to unknown resources are still valuable for
        the inverse lookup direction.)
      * entity: regenerate the UID using this file (entities are file-scoped).
      * technology / service / file / project: use the name verbatim.
    """
    if kind == "resource":
        return name_to_uid.get(name, name)
    if kind == "entity":
        # Best-effort: assume same file. The post-pass for cross-file env-var
        # linking handles the genuine cross-file case explicitly.
        return entity_uid(name, "env_var", file_path)  # rough; works for USES_ENV
    if kind in ("technology", "service", "file", "project"):
        return name.lower() if kind == "technology" else name
    return name


# ---------------------------------------------------------------------------
# Edge post-pass: env_var defs <-> env_ref consumers across files.
# ---------------------------------------------------------------------------

def _build_env_edges(conn: sqlite3.Connection, stats: IndexStats) -> None:
    """For every env_ref entity, find a matching env_var def in another file
    and create a USES_ENV edge from the consuming file to the defining entity.
    """
    refs = conn.execute(
        "SELECT uid, name, file_path FROM entity WHERE entity_type IN ('env_ref','env_use')"
    ).fetchall()
    if not refs:
        return

    # Build def index: env name -> [(uid, file_path, value)].
    defs = conn.execute(
        "SELECT uid, name, file_path, value FROM entity "
        "WHERE entity_type IN ('env_var','env_def')"
    ).fetchall()
    by_name: dict[str, list[sqlite3.Row]] = {}
    for d in defs:
        by_name.setdefault(d["name"], []).append(d)

    created = 0
    for ref in refs:
        candidates = by_name.get(ref["name"], [])
        for d in candidates:
            if d["file_path"] == ref["file_path"]:
                continue  # same-file refs aren't interesting cross-file edges
            upsert_edge(conn, EdgeSpec(
                src_kind="file", src_id=ref["file_path"],
                dst_kind="entity", dst_id=d["uid"],
                rel_type="USES_ENV",
                properties={"env_name": ref["name"], "defined_in": d["file_path"]},
            ))
            created += 1
    stats.edges_created += created


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) or name == p for p in patterns)


def _should_skip(fp: Path, config: "Config") -> bool:
    if _matches_any(fp.name, config.ignore_globs):
        return True
    if fp.suffix.lower() in _BINARY_EXT:
        return True
    return False


def _detect_project(start: Path, markers: list[str]) -> Path | None:
    check = start
    for _ in range(10):
        for marker in markers:
            if (check / marker).exists():
                return check
        parent = check.parent
        if parent == check:
            break
        check = parent
    return None


def _fallback_project(current: Path, scan_root: Path) -> Path | None:
    try:
        rel = current.relative_to(scan_root)
    except ValueError:
        return None
    parts = rel.parts
    if not parts:
        return None
    return scan_root / parts[0]


def _detect_project_type(project_path: Path) -> str:
    markers = {
        "requirements.txt": "python", "pyproject.toml": "python", "setup.py": "python",
        "package.json": "node", "go.mod": "go", "Cargo.toml": "rust",
        "Makefile": "make", "Dockerfile": "docker",
        "docker-compose.yml": "docker-compose", "docker-compose.yaml": "docker-compose",
        "Jenkinsfile": "jenkins", "main.tf": "terraform", "terragrunt.hcl": "terragrunt",
        "Chart.yaml": "helm", "kustomization.yaml": "kustomize",
        "Vagrantfile": "vagrant",
    }
    found = []
    for marker_file, ptype in markers.items():
        if (project_path / marker_file).exists():
            found.append(ptype)
    return "+".join(found) if found else "folder"
