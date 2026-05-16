"""Tests for the Phase 1 fixes:
  - Terraform tag allowlist (skip untaggable)
  - HCL regex precision (exclude var/local/each)
  - infra_context MCP tool (real implementation)
  - service_map MCP tool (real implementation)
"""

from pathlib import Path

import pytest

from engram.graph import (
    EdgeSpec, EntityRow, FileRow, ProjectRow, ResourceRow,
    entity_uid, resource_uid, upsert_edge, upsert_entity, upsert_file,
    upsert_project, upsert_resource,
)


# ---------------------------------------------------------------------------
# P1.1 — Terraform tag allowlist
# ---------------------------------------------------------------------------

def test_taggable_module_classifies_aws_correctly():
    from engram.output.tf_taggable import is_taggable, tag_argument_for
    ok, _ = is_taggable("tf:aws_db_instance")
    assert ok
    ok, reason = is_taggable("tf:aws_iam_role_policy_attachment")
    assert not ok
    assert "tags" in reason or "untaggable" in reason or "fail" in reason
    ok, _ = is_taggable("tf:aws_route_table_association")
    assert not ok
    # GCP uses labels, not tags.
    assert tag_argument_for("tf:google_compute_instance") == "labels"
    assert tag_argument_for("tf:aws_db_instance") == "tags"


def test_taggable_module_rejects_unknown_types():
    """Unknown resource types are skipped (deny by default)."""
    from engram.output.tf_taggable import is_taggable
    ok, reason = is_taggable("tf:totally_made_up_resource")
    assert not ok
    assert "unknown" in reason.lower()


def test_taggable_module_rejects_non_resource_constructs():
    from engram.output.tf_taggable import is_taggable
    ok, _ = is_taggable("tf:variable:foo")
    assert not ok
    ok, _ = is_taggable("tf:module:network")
    assert not ok
    ok, _ = is_taggable("tf:data:aws_ami")
    assert not ok


def test_terraform_plan_skips_untaggable_resources(tmp_db, tmp_path):
    """Annotation plan must NOT include untaggable resource types."""
    from engram.output.annotate import plan_annotations

    fp = tmp_path / "main.tf"
    fp.write_text(
        'resource "aws_db_instance" "ok" { tags = {} }\n'
        'resource "aws_iam_role_policy_attachment" "skip" {}\n'
        'resource "aws_route_table_association" "also_skip" {}\n',
        encoding="utf-8",
    )
    upsert_project(tmp_db, ProjectRow(path="/r", name="r", project_type="terraform"))
    upsert_file(tmp_db, FileRow(
        path=str(fp), project_path="/r", name="main.tf", extension=".tf",
        size_bytes=10, content_hash="x", modified_at="2026-05-14T00:00:00Z",
        risk_tier="red",
    ))
    for kind, name in (
        ("tf:aws_db_instance", "ok"),
        ("tf:aws_iam_role_policy_attachment", "skip"),
        ("tf:aws_route_table_association", "also_skip"),
    ):
        uid = resource_uid(kind, name, "", str(fp))
        upsert_resource(tmp_db, ResourceRow(
            uid=uid, file_path=str(fp), kind=kind, name=name,
            environment="production", risk_tier="red",
        ))

    plan = plan_annotations(tmp_db, target_kind="terraform")
    op_names = {op.rationale.split(" ")[1] for op in plan.ops}
    assert "ok" in op_names
    assert "skip" not in op_names
    assert "also_skip" not in op_names
    # Skips logged.
    assert len(plan.skipped) == 2


# ---------------------------------------------------------------------------
# P1.2 — HCL reference regex precision
# ---------------------------------------------------------------------------

def test_terraform_ignores_var_local_each():
    from engram.extractors.terraform_ext import TerraformExtractor
    content = (
        'resource "aws_instance" "web" {\n'
        '  ami           = var.ami_id\n'
        '  instance_type = local.size\n'
        '  subnet_id     = each.value.subnet\n'
        '  count_index   = count.index\n'
        '  tags          = merge(local.tags, { Name = "web" })\n'
        '}\n'
    )
    res = TerraformExtractor().extract(Path("infra/main.tf"), content)
    # The only edges should be the provider USES tech edge, NOT spurious
    # DEPENDS_ON edges to var/local/each/count/merge.
    depends = [e for e in res.edges if e.rel_type == "DEPENDS_ON"]
    bad_targets = {"ami_id", "size", "value", "subnet", "index", "tags"}
    assert not any(e.target_name in bad_targets for e in depends), (
        f"spurious DEPENDS_ON edges: {[e.target_name for e in depends]}"
    )


def test_terraform_keeps_real_resource_refs():
    from engram.extractors.terraform_ext import TerraformExtractor
    content = (
        'resource "aws_db_instance" "prod_db" {}\n'
        'resource "aws_ecs_service" "api" {\n'
        '  depends_on = [aws_db_instance.prod_db]\n'
        '  cluster    = aws_ecs_cluster.main.id\n'
        '}\n'
        'resource "aws_ecs_cluster" "main" {}\n'
    )
    res = TerraformExtractor().extract(Path("infra/main.tf"), content)
    depends = [e for e in res.edges
               if e.rel_type == "DEPENDS_ON" and e.source_name == "api"]
    targets = {e.target_name for e in depends}
    # api depends on the DB (explicit) and the cluster (implicit ref).
    assert "prod_db" in targets
    assert "main" in targets


# ---------------------------------------------------------------------------
# P1.3 — infra_context
# ---------------------------------------------------------------------------

def _seed_payments_service(conn, tmp_path):
    """Seed a payments service across 3 formats with env-var cross-refs."""
    upsert_project(conn, ProjectRow(path="/r", name="r", project_type="docker"))

    fp_docker = tmp_path / "Dockerfile"
    fp_docker.write_text("FROM python:3.12", encoding="utf-8")
    fp_k8s = tmp_path / "k8s.yaml"
    fp_k8s.write_text("apiVersion: apps/v1\nkind: Deployment", encoding="utf-8")
    fp_tf = tmp_path / "main.tf"
    fp_tf.write_text('resource "aws_ecs_service" "payments" {}', encoding="utf-8")

    for fp in (fp_docker, fp_k8s, fp_tf):
        upsert_file(conn, FileRow(
            path=str(fp), project_path="/r", name=fp.name, extension=fp.suffix,
            size_bytes=10, content_hash=fp.name, modified_at="2026-05-14T00:00:00Z",
            risk_tier="red",
        ))

    docker_uid = resource_uid("docker:image", "payments", "", str(fp_docker))
    k8s_uid = resource_uid("k8s:Deployment", "payments", "prod", str(fp_k8s))
    tf_uid = resource_uid("tf:aws_ecs_service", "payments", "", str(fp_tf))
    db_uid = resource_uid("tf:aws_db_instance", "prod_db", "", str(fp_tf))

    for uid, kind, fp, ns, env in (
        (docker_uid, "docker:image", fp_docker, "", "production"),
        (k8s_uid, "k8s:Deployment", fp_k8s, "prod", "production"),
        (tf_uid, "tf:aws_ecs_service", fp_tf, "", "production"),
        (db_uid, "tf:aws_db_instance", fp_tf, "", "production"),
    ):
        upsert_resource(conn, ResourceRow(
            uid=uid, file_path=str(fp), kind=kind, name="payments" if uid != db_uid else "prod_db",
            namespace=ns, environment=env, risk_tier="red",
        ))

    upsert_edge(conn, EdgeSpec(
        src_kind="resource", src_id=tf_uid, dst_kind="resource", dst_id=db_uid,
        rel_type="DEPENDS_ON",
    ))

    # Env-var def in docker, refs in k8s.
    def_uid = entity_uid("DATABASE_URL", "env_var", str(fp_docker))
    upsert_entity(conn, EntityRow(
        uid=def_uid, file_path=str(fp_docker),
        name="DATABASE_URL", entity_type="env_var", value="postgres://x",
    ))
    ref_uid = entity_uid("DATABASE_URL", "env_ref", str(fp_k8s))
    upsert_entity(conn, EntityRow(
        uid=ref_uid, file_path=str(fp_k8s),
        name="DATABASE_URL", entity_type="env_ref",
    ))

    return [str(fp_docker), str(fp_k8s), str(fp_tf)]


def test_infra_context_returns_five_sections(tmp_db, tmp_path):
    from engram.mcp_tools.context import build_infra_context
    _seed_payments_service(tmp_db, tmp_path)
    payload = build_infra_context(tmp_db, "payments", token_budget=4000)
    assert payload["found"] is True
    assert payload["target"] == "payments"
    for section in ("summary", "resources", "env_vars", "dependencies", "recent_changes"):
        assert section in payload, f"missing section: {section}"
    assert payload["summary"]["resource_count"] >= 3
    assert "production" in payload["summary"]["environments"]
    assert payload["summary"]["risk_tier"] == "red"


def test_infra_context_not_found_when_target_missing(tmp_db, tmp_path):
    from engram.mcp_tools.context import build_infra_context
    payload = build_infra_context(tmp_db, "does_not_exist", token_budget=2000)
    assert payload["found"] is False
    assert "hint" in payload


def test_infra_context_compresses_under_budget(tmp_db, tmp_path):
    """Tighter budget produces strictly smaller (or equal) payload."""
    import json
    from engram.mcp_tools.context import build_infra_context
    _seed_payments_service(tmp_db, tmp_path)
    big = build_infra_context(tmp_db, "payments", token_budget=10000)
    small = build_infra_context(tmp_db, "payments", token_budget=100)
    big_size = len(json.dumps(big))
    small_size = len(json.dumps(small))
    assert small_size <= big_size
    # Compression markers appear when budget is tight.
    assert "_compression" in small or small_size <= 400


def test_infra_context_cross_file_env_vars(tmp_db, tmp_path):
    """A ref in one file with a def in another should appear in external_definitions."""
    from engram.mcp_tools.context import build_infra_context
    _seed_payments_service(tmp_db, tmp_path)
    payload = build_infra_context(tmp_db, "payments", token_budget=8000)
    env = payload.get("env_vars", {})
    # Either the def is in the resolved files (so it's in `defined`), or the
    # ref points to it across files (so it's in `external_definitions`).
    names = (
        {e["name"] for e in env.get("defined", [])}
        | {e["name"] for e in env.get("referenced", [])}
        | {e["name"] for e in env.get("external_definitions", [])}
    )
    assert "DATABASE_URL" in names


# ---------------------------------------------------------------------------
# P1.4 — service_map
# ---------------------------------------------------------------------------

def test_service_map_emits_adjacency_and_mermaid(tmp_db, tmp_path):
    from engram.mcp_tools.service_map import build_service_map
    _seed_payments_service(tmp_db, tmp_path)
    smap = build_service_map(tmp_db, scope="all")
    assert smap["found"] is True
    # The payments service depends on prod_db.
    assert "payments" in smap["adjacency"]
    assert "prod_db" in smap["adjacency"]["payments"]
    # Mermaid is non-empty and has graph header.
    assert smap["mermaid"].startswith("graph ")
    assert "n_payments" in smap["mermaid"]


def test_service_map_scope_to_single_service(tmp_db, tmp_path):
    from engram.mcp_tools.service_map import build_service_map
    _seed_payments_service(tmp_db, tmp_path)
    smap = build_service_map(tmp_db, scope="payments")
    assert smap["found"] is True
    # Scoped view should still include the dependency.
    assert "payments" in smap["adjacency"] or "payments" in smap["nodes"]


def test_service_map_missing_service_returns_not_found(tmp_db, tmp_path):
    from engram.mcp_tools.service_map import build_service_map
    smap = build_service_map(tmp_db, scope="does_not_exist")
    assert smap["found"] is False
