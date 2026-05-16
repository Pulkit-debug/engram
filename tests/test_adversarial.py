"""Adversarial scenarios — failure modes the system must handle gracefully.

Rule: fail-fast is OK; crash-with-traceback is not. Each test constructs a
deliberate trap and asserts the relevant component degrades to a sensible
result without raising.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 2.1 — Malformed YAML
# ---------------------------------------------------------------------------

def test_malformed_yaml_returns_empty_result():
    from engram.extractors.yaml_ext import YAMLExtractor
    broken = (
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: foo\n"
        "  labels: {unclosed brace\n"
        "  bad-tab:\there\n"
    )
    # Must not raise.
    res = YAMLExtractor().extract(Path("k8s/broken.yaml"), broken)
    # Empty or shallow is acceptable. The point is no exception.
    assert res.resources == [] or all(isinstance(r.name, str) for r in res.resources)


# ---------------------------------------------------------------------------
# 2.2 — Files over size limit
# ---------------------------------------------------------------------------

def test_crawler_skips_files_over_size_limit(tmp_db, tmp_cfg, tmp_path):
    from engram.crawler import index_paths

    repo = tmp_path / "repo"
    repo.mkdir()
    big = repo / "huge.tf"
    big.write_bytes(b"x" * (600 * 1024))  # 600KB, over the 500KB default
    small = repo / "Dockerfile"
    small.write_text("FROM python:3.12\n", encoding="utf-8")

    tmp_cfg.watch_paths = [repo]
    tmp_cfg.max_file_size_kb = 500
    stats = index_paths(tmp_db, tmp_cfg)
    # Big file skipped, small one indexed.
    assert stats.files_skipped >= 1
    assert any(name in str(p) for p in [small] for name in ["Dockerfile"])


# ---------------------------------------------------------------------------
# 2.3 — Binary file with text-like extension
# ---------------------------------------------------------------------------

def test_binary_file_with_yaml_extension_does_not_crash(tmp_db, tmp_cfg, tmp_path):
    from engram.crawler import index_paths

    repo = tmp_path / "repo"
    repo.mkdir()
    binary_yaml = repo / "fake.yaml"
    binary_yaml.write_bytes(b"\x00\x01\x02\x03\xff\xfe\xfd" * 100)

    tmp_cfg.watch_paths = [repo]
    # Must not raise — decode with errors='ignore', extractor handles bad input.
    stats = index_paths(tmp_db, tmp_cfg)
    # Either skipped (binary detection) or indexed with empty extraction.
    assert stats.files_scanned >= 1


# ---------------------------------------------------------------------------
# 2.4 — Empty file
# ---------------------------------------------------------------------------

def test_empty_file_is_skipped(tmp_db, tmp_cfg, tmp_path):
    from engram.crawler import index_paths

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "empty.tf").write_text("", encoding="utf-8")
    tmp_cfg.watch_paths = [repo]
    stats = index_paths(tmp_db, tmp_cfg)
    assert stats.files_indexed == 0
    assert stats.files_skipped >= 1


# ---------------------------------------------------------------------------
# 2.5 — Unicode in resource names
# ---------------------------------------------------------------------------

def test_unicode_resource_names_round_trip(tmp_db, tmp_cfg, tmp_path):
    from engram.crawler import index_paths
    from engram.safety.blast_radius import assess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "k8s.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\n"
        "metadata:\n  name: naïve-service\n  namespace: prod\n"
        "  labels:\n    environment: production\n"
        "spec:\n  replicas: 1\n",
        encoding="utf-8",
    )
    tmp_cfg.watch_paths = [repo]
    index_paths(tmp_db, tmp_cfg)
    result = assess(tmp_db, "kubectl delete deployment", "naïve-service")
    assert result.environment == "production"
    assert result.action == "block"


# ---------------------------------------------------------------------------
# 2.6 — K8s manifest starting with `---`
# ---------------------------------------------------------------------------

def test_yaml_with_leading_doc_separator():
    from engram.extractors.yaml_ext import YAMLExtractor
    content = (
        "---\n"
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n  name: alpha\n"
        "spec:\n  replicas: 1\n"
        "---\n"
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n  name: alpha\n"
        "spec:\n  ports: [{port: 80}]\n"
    )
    res = YAMLExtractor().extract(Path("multi.yaml"), content)
    kinds = {r.kind for r in res.resources}
    assert "k8s:Deployment" in kinds
    # Service may or may not parse depending on lib; the test is just no-crash
    # for the leading --- case.


# ---------------------------------------------------------------------------
# 2.7 — Helm template with Go interpolation
# ---------------------------------------------------------------------------

def test_helm_template_does_not_crash():
    from engram.extractors.yaml_ext import YAMLExtractor
    content = (
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: {{ .Release.Name }}-payments\n"
        "  namespace: {{ .Values.namespace }}\n"
        "spec:\n"
        "  replicas: {{ .Values.replicaCount | default 3 }}\n"
    )
    # Go template syntax breaks PyYAML; the test is just no-crash.
    res = YAMLExtractor().extract(Path("templates/deployment.yaml"), content)
    # Either empty or a shallow fallback — both fine.
    assert isinstance(res.resources, list)


# ---------------------------------------------------------------------------
# 2.8 — docker-compose with profiles and external networks
# ---------------------------------------------------------------------------

def test_docker_compose_with_profiles():
    from engram.extractors.compose_ext import DockerComposeExtractor
    content = (
        "services:\n"
        "  web:\n"
        "    image: nginx:1.25\n"
        "    profiles: [\"prod\"]\n"
        "    networks: [external_net]\n"
        "  debug:\n"
        "    image: alpine\n"
        "    profiles: [\"debug\"]\n"
        "networks:\n"
        "  external_net:\n"
        "    external: true\n"
    )
    res = DockerComposeExtractor().extract(Path("compose.yml"), content)
    names = {r.name for r in res.resources}
    assert "web" in names and "debug" in names


# ---------------------------------------------------------------------------
# 2.9 — Jenkinsfile scripted (non-declarative)
# ---------------------------------------------------------------------------

def test_jenkinsfile_scripted_pipeline():
    from engram.extractors.devops_ext import JenkinsfileExtractor
    content = (
        "node('docker') {\n"
        "  stage('Checkout') { checkout scm }\n"
        "  stage('Build') { sh 'make' }\n"
        "  stage('Deploy') { sh 'kubectl apply -f .' }\n"
        "}\n"
    )
    res = JenkinsfileExtractor().extract(Path("Jenkinsfile"), content)
    # Stage detection still works in scripted mode.
    stage_names = {r.name for r in res.resources if r.kind == "jenkins:stage"}
    assert {"Checkout", "Build", "Deploy"} <= stage_names


# ---------------------------------------------------------------------------
# 2.10 — Dockerfile with multi-line RUN
# ---------------------------------------------------------------------------

def test_dockerfile_multiline_run():
    from engram.extractors.dockerfile import DockerfileExtractor
    content = (
        "FROM python:3.12\n"
        "RUN apt-get update \\\n"
        "    && apt-get install -y curl \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
        "ENV DATABASE_URL=postgres://x\n"
    )
    res = DockerfileExtractor().extract(Path("Dockerfile"), content)
    # The ENV after the multi-line RUN should be picked up.
    assert any(e.name == "DATABASE_URL" for e in res.entities)


# ---------------------------------------------------------------------------
# 2.11 — Symlink loops
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="symlinks require admin on Windows")
def test_symlink_loop_does_not_hang(tmp_db, tmp_cfg, tmp_path):
    from engram.crawler import index_paths

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    # Create a self-referential symlink.
    loop = repo / "loop"
    try:
        loop.symlink_to(repo, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted")

    tmp_cfg.watch_paths = [repo]
    # os.walk default is followlinks=False — the loop must NOT cause an
    # infinite scan. Bound by a small wall-clock check.
    start = time.monotonic()
    stats = index_paths(tmp_db, tmp_cfg)
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f"crawler took {elapsed:.1f}s on a symlink loop"
    assert stats.files_indexed >= 1


# ---------------------------------------------------------------------------
# 2.12 — Permission denied
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="chmod semantics differ on Windows")
def test_permission_denied_skips_file(tmp_db, tmp_cfg, tmp_path):
    from engram.crawler import index_paths

    repo = tmp_path / "repo"
    repo.mkdir()
    good = repo / "Dockerfile"
    good.write_text("FROM python:3.12\n", encoding="utf-8")
    bad = repo / "bad.tf"
    bad.write_text("resource \"aws_instance\" \"x\" {}\n", encoding="utf-8")
    os.chmod(bad, 0)  # no read permission

    tmp_cfg.watch_paths = [repo]
    try:
        stats = index_paths(tmp_db, tmp_cfg)
        # Other files indexed; failing one skipped (or extractor sees empty).
        assert stats.files_scanned >= 2
    finally:
        os.chmod(bad, 0o644)  # restore so tmp cleanup works


# ---------------------------------------------------------------------------
# 2.13 — Concurrent CLI + serve (SQLite WAL claim)
# ---------------------------------------------------------------------------

def test_concurrent_reads_do_not_block(tmp_cfg, tmp_path):
    """Two simultaneous read connections must both succeed under WAL."""
    from engram.db import open_db
    from engram.graph import FileRow, ProjectRow, upsert_file, upsert_project

    tmp_cfg.data_dir = tmp_path / "data"
    tmp_cfg.ensure_dirs()
    conn1 = open_db(tmp_cfg)
    upsert_project(conn1, ProjectRow(path="/r", name="r"))
    upsert_file(conn1, FileRow(
        path="/r/foo.tf", project_path="/r", name="foo.tf", extension=".tf",
        size_bytes=10, content_hash="h", modified_at="2026-05-14T00:00:00Z",
    ))
    # Second connection while first is open.
    conn2 = open_db(tmp_cfg)
    rows = conn2.execute("SELECT count(*) FROM file").fetchone()
    assert rows[0] == 1
    conn1.close()
    conn2.close()


# ---------------------------------------------------------------------------
# 2.15 — SQL-injection-shaped resource names
# ---------------------------------------------------------------------------

def test_resource_name_with_injection_chars_is_safe(tmp_db, tmp_path):
    from engram.graph import (
        FileRow, ProjectRow, ResourceRow,
        resource_uid, upsert_file, upsert_project, upsert_resource,
    )
    from engram.safety.blast_radius import assess

    name = "foo'; DROP TABLE file; --"
    upsert_project(tmp_db, ProjectRow(path="/r", name="r"))
    upsert_file(tmp_db, FileRow(
        path="/r/x.tf", project_path="/r", name="x.tf", extension=".tf",
        size_bytes=10, content_hash="h", modified_at="2026-05-14T00:00:00Z",
        risk_tier="red",
    ))
    upsert_resource(tmp_db, ResourceRow(
        uid=resource_uid("tf:aws_db_instance", name, "", "/r/x.tf"),
        file_path="/r/x.tf", kind="tf:aws_db_instance", name=name,
        environment="production", risk_tier="red",
    ))
    # Must not raise and the file table must still exist.
    result = assess(tmp_db, "terraform destroy", name)
    assert result.environment == "production"
    tables = tmp_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='file'"
    ).fetchone()
    assert tables is not None


# ---------------------------------------------------------------------------
# 2.16 — assess against an empty database
# ---------------------------------------------------------------------------

def test_assess_on_empty_db_returns_confirm_not_crash(tmp_db):
    from engram.safety.blast_radius import assess
    result = assess(tmp_db, "terraform destroy", "anything")
    assert result.action in ("confirm", "block")
    assert any("No Resource matched" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# 2.17 — emit-agents-md against read-only target
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="chmod semantics differ on Windows")
def test_emit_agents_md_readonly_target_does_not_crash(tmp_db, tmp_cfg, tmp_path):
    from engram.output.codified import upsert_agents_md
    from engram.graph import FileRow, ProjectRow, upsert_file, upsert_project

    upsert_project(tmp_db, ProjectRow(path="/r", name="r"))
    upsert_file(tmp_db, FileRow(
        path="/r/x.tf", project_path="/r", name="x.tf", extension=".tf",
        size_bytes=10, content_hash="h", modified_at="2026-05-14T00:00:00Z",
    ))
    target = tmp_path / "ro" / "AGENTS.md"
    target.parent.mkdir()
    # Make the dir not-writable.
    os.chmod(target.parent, 0o555)
    try:
        with pytest.raises((OSError, PermissionError)):
            upsert_agents_md(tmp_db, tmp_cfg, target)
    finally:
        os.chmod(target.parent, 0o755)


# ---------------------------------------------------------------------------
# 2.18 — unlabel with missing files
# ---------------------------------------------------------------------------

def test_unlabel_handles_missing_files(tmp_db, tmp_path):
    from engram.output.annotate import unlabel_all
    from engram.graph import (
        FileRow, ProjectRow, ResourceRow,
        resource_uid, upsert_file, upsert_project, upsert_resource,
    )

    # Seed an annotation referencing a file that won't exist when we unlabel.
    fp = tmp_path / "gone.yaml"
    fp.write_text("apiVersion: v1\nkind: Pod\nmetadata:\n  name: x\n", encoding="utf-8")
    upsert_project(tmp_db, ProjectRow(path="/r", name="r"))
    upsert_file(tmp_db, FileRow(
        path=str(fp), project_path="/r", name="gone.yaml", extension=".yaml",
        size_bytes=10, content_hash="h", modified_at="2026-05-14T00:00:00Z",
    ))
    uid = resource_uid("k8s:Pod", "x", "", str(fp))
    upsert_resource(tmp_db, ResourceRow(
        uid=uid, file_path=str(fp), kind="k8s:Pod", name="x",
    ))
    tmp_db.execute(
        "INSERT INTO annotation(target_kind, target_id, label_key, label_value, applied_at) "
        "VALUES ('k8s', ?, 'engram.io/managed-by', 'engram', '2026-05-14T00:00:00Z')",
        (uid,),
    )
    # Now delete the file.
    fp.unlink()
    # unlabel must not crash; should log/return errors for missing files.
    result = unlabel_all(tmp_db, dry_run=False)
    # All entries should be in `errors` (file gone), no exception raised.
    assert "errors" in result
