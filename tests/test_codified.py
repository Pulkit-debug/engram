"""Tests for codified-context emission and drift detection."""

from pathlib import Path

from engram.graph import (
    FileRow, ProjectRow, ResourceRow,
    resource_uid, upsert_file, upsert_project, upsert_resource,
)
from engram.output.codified import (
    detect_drift, sign_section, upsert_agents_md, verify_section,
    ENGRAM_START, ENGRAM_END,
)


def _seed_one(conn):
    upsert_project(conn, ProjectRow(path="/r", name="r", project_type="terraform"))
    upsert_file(conn, FileRow(
        path="/r/main.tf", project_path="/r", name="main.tf", extension=".tf",
        size_bytes=10, content_hash="x", modified_at="2026-05-14T00:00:00Z",
        risk_tier="red",
    ))
    uid = resource_uid("tf:aws_db_instance", "db", "", "/r/main.tf")
    upsert_resource(conn, ResourceRow(
        uid=uid, file_path="/r/main.tf", kind="tf:aws_db_instance",
        name="db", environment="production", risk_tier="red",
    ))


def test_sign_and_verify_roundtrip(tmp_cfg):
    sig = sign_section("hello", tmp_cfg)
    assert verify_section("hello", sig, tmp_cfg) is True
    assert verify_section("hello tampered", sig, tmp_cfg) is False


def test_emit_creates_and_check_drift_clean(tmp_db, tmp_cfg, tmp_path):
    _seed_one(tmp_db)
    target = tmp_path / "AGENTS.md"
    result = upsert_agents_md(tmp_db, tmp_cfg, target)
    assert result["action"] == "created"
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert ENGRAM_START in text and ENGRAM_END in text
    # immediate re-check should be clean
    drift = detect_drift(tmp_db, tmp_cfg, target)
    assert drift["drift"] is False


def test_tamper_detection_refuses_overwrite(tmp_db, tmp_cfg, tmp_path):
    _seed_one(tmp_db)
    target = tmp_path / "AGENTS.md"
    upsert_agents_md(tmp_db, tmp_cfg, target)
    # Tamper with the engram-managed section.
    text = target.read_text(encoding="utf-8")
    tampered = text.replace("## Infrastructure overview", "## TAMPERED")
    target.write_text(tampered, encoding="utf-8")
    # Re-emit without --force should refuse.
    result = upsert_agents_md(tmp_db, tmp_cfg, target, force=False)
    assert result["action"] == "tampered"


def test_tamper_force_overwrite(tmp_db, tmp_cfg, tmp_path):
    _seed_one(tmp_db)
    target = tmp_path / "AGENTS.md"
    upsert_agents_md(tmp_db, tmp_cfg, target)
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("Infrastructure overview", "TAMPERED OVERVIEW"),
                      encoding="utf-8")
    result = upsert_agents_md(tmp_db, tmp_cfg, target, force=True)
    assert result["action"] in ("updated", "unchanged")
    assert "Infrastructure overview" in target.read_text(encoding="utf-8")


def test_emit_preserves_human_curated_content(tmp_db, tmp_cfg, tmp_path):
    _seed_one(tmp_db)
    target = tmp_path / "AGENTS.md"
    target.write_text(
        "# My Project\n\n## Human-written rules\n\n- Always use kebab-case for K8s names.\n",
        encoding="utf-8",
    )
    upsert_agents_md(tmp_db, tmp_cfg, target)
    text = target.read_text(encoding="utf-8")
    assert "Always use kebab-case" in text  # human content preserved
    assert ENGRAM_START in text             # engram block appended
