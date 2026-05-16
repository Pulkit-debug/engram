"""Tests for the infrastructure annotator (Mode C)."""

from pathlib import Path

from engram.graph import (
    FileRow, ProjectRow, ResourceRow,
    resource_uid, upsert_file, upsert_project, upsert_resource,
)
from engram.output.annotate import (
    apply_plan, plan_annotations, unlabel_all, ENGRAM_LABEL_PREFIX,
)


def _seed_k8s(conn, file_path: str):
    upsert_project(conn, ProjectRow(path="/r", name="r", project_type="k8s"))
    upsert_file(conn, FileRow(
        path=file_path, project_path="/r", name="payments.yaml", extension=".yaml",
        size_bytes=200, content_hash="h", modified_at="2026-05-14T00:00:00Z",
        risk_tier="red",
    ))
    uid = resource_uid("k8s:Deployment", "payments", "prod", file_path)
    upsert_resource(conn, ResourceRow(
        uid=uid, file_path=file_path, kind="k8s:Deployment",
        name="payments", namespace="prod",
        environment="production", risk_tier="red",
    ))
    return uid


def test_plan_includes_k8s_resources(tmp_db, tmp_path):
    fp = tmp_path / "payments.yaml"
    fp.write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: payments\n"
        "  namespace: prod\n  labels:\n    app: payments\nspec: {}\n",
        encoding="utf-8",
    )
    _seed_k8s(tmp_db, str(fp))
    plan = plan_annotations(tmp_db, target_kind="k8s")
    assert len(plan.ops) == 1
    assert plan.ops[0].target_kind == "k8s"
    assert "engram.io/managed-by" in plan.ops[0].labels


def test_apply_inserts_labels_into_existing_block(tmp_db, tmp_path):
    fp = tmp_path / "payments.yaml"
    fp.write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: payments\n"
        "  namespace: prod\n  labels:\n    app: payments\nspec: {}\n",
        encoding="utf-8",
    )
    _seed_k8s(tmp_db, str(fp))
    plan = plan_annotations(tmp_db, target_kind="k8s")
    result = apply_plan(tmp_db, plan, dry_run=False)
    assert fp.as_posix() in [Path(p).as_posix() for p in result["changed"]]
    text = fp.read_text(encoding="utf-8")
    assert "engram.io/risk-tier" in text
    assert "engram.io/environment" in text
    # Original labels intact.
    assert "app: payments" in text


def test_apply_idempotent(tmp_db, tmp_path):
    fp = tmp_path / "payments.yaml"
    fp.write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: payments\n"
        "  namespace: prod\n  labels:\n    app: payments\nspec: {}\n",
        encoding="utf-8",
    )
    _seed_k8s(tmp_db, str(fp))
    plan = plan_annotations(tmp_db, target_kind="k8s")
    apply_plan(tmp_db, plan, dry_run=False)
    text1 = fp.read_text(encoding="utf-8")
    apply_plan(tmp_db, plan, dry_run=False)
    text2 = fp.read_text(encoding="utf-8")
    # Idempotency: second apply does not duplicate engram.io/* keys.
    assert text1.count("engram.io/risk-tier") == 1
    assert text2.count("engram.io/risk-tier") == 1


def test_unlabel_removes_everything(tmp_db, tmp_path):
    fp = tmp_path / "payments.yaml"
    fp.write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: payments\n"
        "  namespace: prod\n  labels:\n    app: payments\nspec: {}\n",
        encoding="utf-8",
    )
    _seed_k8s(tmp_db, str(fp))
    plan = plan_annotations(tmp_db, target_kind="k8s")
    apply_plan(tmp_db, plan, dry_run=False)
    assert "engram.io/" in fp.read_text(encoding="utf-8")
    unlabel_all(tmp_db, dry_run=False)
    assert "engram.io/" not in fp.read_text(encoding="utf-8")


def test_terraform_tag_injection(tmp_db, tmp_path):
    fp = tmp_path / "main.tf"
    fp.write_text(
        'resource "aws_db_instance" "prod_db" {\n'
        '  identifier = "prod-db"\n'
        '  engine     = "postgres"\n'
        '  tags = {\n'
        '    Environment = "production"\n'
        '  }\n'
        '}\n',
        encoding="utf-8",
    )
    upsert_project(tmp_db, ProjectRow(path="/r", name="r", project_type="terraform"))
    upsert_file(tmp_db, FileRow(
        path=str(fp), project_path="/r", name="main.tf", extension=".tf",
        size_bytes=200, content_hash="h", modified_at="2026-05-14T00:00:00Z",
        risk_tier="red",
    ))
    uid = resource_uid("tf:aws_db_instance", "prod_db", "", str(fp))
    upsert_resource(tmp_db, ResourceRow(
        uid=uid, file_path=str(fp), kind="tf:aws_db_instance",
        name="prod_db", environment="production", risk_tier="red",
    ))
    plan = plan_annotations(tmp_db, target_kind="terraform")
    assert plan.ops
    apply_plan(tmp_db, plan, dry_run=False)
    text = fp.read_text(encoding="utf-8")
    assert '"engram.io/risk-tier" = "red"' in text
    assert 'Environment = "production"' in text  # original preserved
