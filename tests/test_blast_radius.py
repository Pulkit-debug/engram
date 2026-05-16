"""Tests for the blast-radius primitive."""

from engram.graph import (
    EdgeSpec, FileRow, ProjectRow, ResourceRow,
    resource_uid, upsert_edge, upsert_file, upsert_project, upsert_resource,
)
from engram.safety.blast_radius import (
    assess, classify_operation, infer_environment_from_path, tier_for_environment,
)


def _seed_prod_db(conn):
    upsert_project(conn, ProjectRow(path="/repo/x", name="x", project_type="terraform"))
    upsert_file(conn, FileRow(
        path="/repo/x/prod/main.tf", project_path="/repo/x", name="main.tf",
        extension=".tf", size_bytes=100, content_hash="h", modified_at="2026-05-14T00:00:00Z",
        risk_tier="red",
    ))
    uid = resource_uid("tf:aws_db_instance", "prod_db", "", "/repo/x/prod/main.tf")
    upsert_resource(conn, ResourceRow(
        uid=uid, file_path="/repo/x/prod/main.tf",
        kind="tf:aws_db_instance", name="prod_db",
        environment="production", risk_tier="red",
    ))
    # 3 dependent services
    for i, svc in enumerate(["api", "worker", "scheduler"]):
        upsert_file(conn, FileRow(
            path=f"/repo/x/k8s/{svc}.yaml", project_path="/repo/x", name=f"{svc}.yaml",
            extension=".yaml", size_bytes=100, content_hash=str(i), modified_at="2026-05-14T00:00:00Z",
            risk_tier="orange",
        ))
        svc_uid = resource_uid("k8s:Deployment", svc, "prod", f"/repo/x/k8s/{svc}.yaml")
        upsert_resource(conn, ResourceRow(
            uid=svc_uid, file_path=f"/repo/x/k8s/{svc}.yaml",
            kind="k8s:Deployment", name=svc, environment="production", risk_tier="orange",
        ))
        upsert_edge(conn, EdgeSpec(
            src_kind="resource", src_id=svc_uid,
            dst_kind="resource", dst_id=uid,
            rel_type="DEPENDS_ON",
        ))
    return uid


def test_classify_operation_destructive():
    assert classify_operation("terraform destroy") == "destructive"
    assert classify_operation("kubectl delete deployment foo") == "destructive"
    assert classify_operation("rm -rf /") == "destructive"
    assert classify_operation("helm uninstall api") == "destructive"


def test_classify_operation_mutating():
    assert classify_operation("terraform apply") == "mutating"
    assert classify_operation("kubectl apply -f x.yaml") == "mutating"


def test_classify_operation_read():
    assert classify_operation("kubectl get pods") == "read"
    assert classify_operation("terraform plan") == "read"


def test_infer_environment_from_path():
    assert infer_environment_from_path("/repo/infra/prod/main.tf") == "production"
    assert infer_environment_from_path("/repo/infra/staging/main.tf") == "staging"
    assert infer_environment_from_path("/repo/local/dev.tf") == "dev"
    assert infer_environment_from_path("/repo/random.tf") == ""


def test_tier_for_environment():
    assert tier_for_environment("production") == "red"
    assert tier_for_environment("staging") == "orange"
    assert tier_for_environment("") == "green"


def test_assess_destructive_on_production_is_block(tmp_db):
    _seed_prod_db(tmp_db)
    result = assess(tmp_db, "terraform destroy", "prod_db")
    assert result.action == "block"
    assert result.risk_tier == "red"
    assert result.environment == "production"
    assert len(result.resolved_resources) == 1
    # 3 dependents seeded
    assert len(result.dependents) == 3


def test_assess_read_on_production_is_proceed(tmp_db):
    _seed_prod_db(tmp_db)
    result = assess(tmp_db, "kubectl get deployment", "prod_db")
    assert result.action == "proceed"
    assert result.risk_tier == "green"


def test_assess_unknown_target_is_confirm(tmp_db):
    result = assess(tmp_db, "terraform destroy", "non_existent_resource")
    # No resource resolved, destructive op — bias to confirm (not silently proceed).
    assert result.action in ("confirm", "block")
    assert any("No Resource matched" in r for r in result.reasons)


def test_assess_destructive_destination_propagates_dependents(tmp_db):
    _seed_prod_db(tmp_db)
    # destroying the DB returns its 3 dependents
    result = assess(tmp_db, "terraform destroy", "prod_db")
    dep_kinds = {d["kind"] for d in result.dependents}
    assert "resource" in dep_kinds
