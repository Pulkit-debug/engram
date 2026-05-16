"""Tests for the PreToolUse hook — the harness-layer enforcement primitive."""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import pytest

from engram.hook.classifier import classify


# ---------------------------------------------------------------------------
# Classifier — covers every important command shape
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,expected_op,expected_target", [
    # terraform
    ("terraform destroy", "terraform destroy", ""),
    ("terraform destroy -target=aws_db_instance.prod", "terraform destroy", "aws_db_instance.prod"),
    ("terraform apply -auto-approve", "terraform apply", ""),
    ("terraform plan", "terraform plan", ""),
    ("tofu destroy", "tofu destroy", ""),

    # kubectl
    ("kubectl delete deployment payments", "kubectl delete deployment", "payments"),
    ("kubectl delete deployment payments -n prod", "kubectl delete deployment", "payments"),
    ("kubectl apply -f manifest.yaml", "kubectl apply", ""),
    # Note: the kind is included in the operation string — `kubectl get pods` not just `kubectl get`.
    ("kubectl get pods", "kubectl get pods", ""),
    ("oc delete pod foo", "kubectl delete pod", "foo"),

    # helm
    ("helm uninstall vault", "helm uninstall", "vault"),
    ("helm install my-release ./chart", "helm install", "my-release"),
    ("helm rollback vault 3", "helm rollback", "vault"),

    # docker
    ("docker rm -f foo", "docker rm", "foo"),
    ("docker compose down", "docker compose down", ""),
    ("docker volume rm myvol", "docker volume", "rm"),  # naive — but flagged as infra

    # aws
    ("aws rds delete-db-instance --db-instance-identifier payments-prod",
     "aws rds delete-db-instance", "payments-prod"),
    ("aws s3 rb s3://my-bucket --force", "aws s3 rb", ""),
    ("aws ec2 terminate-instances --instance-ids i-abc",
     "aws ec2 terminate-instances", "i-abc"),
    ("aws lambda delete-function --function-name foo",
     "aws lambda delete-function", "foo"),

    # gcloud / az
    ("gcloud compute instances delete my-vm",
     "gcloud compute instances delete", "my-vm"),
    ("az group delete --name rg-prod", "az group delete", ""),

    # rm
    ("rm -rf /tmp/build", "rm -rf", "/tmp/build"),
    ("rm -rf /etc/shadow", "rm -rf", "/etc/shadow"),

    # pulumi
    ("pulumi destroy", "pulumi destroy", ""),
])
def test_classifier_known_destructive(cmd: str, expected_op: str, expected_target: str):
    intent = classify(cmd)
    assert intent.is_infra_command, f"expected is_infra=True for {cmd!r}"
    assert intent.operation == expected_op, f"op mismatch for {cmd!r}"
    if expected_target:
        assert intent.target == expected_target, f"target mismatch for {cmd!r}"


@pytest.mark.parametrize("cmd", [
    "ls -la",
    "cat /etc/hosts",
    "echo hello",
    "git status",
    "git push origin main",
    "npm test",
    "pytest",
    "python script.py",
    "make build",
    "curl https://example.com",
    "rm foo.txt",          # no -r/-f → not infra-y
    "",
    "   ",
])
def test_classifier_non_infra(cmd: str):
    intent = classify(cmd)
    assert not intent.is_infra_command, f"expected non-infra for {cmd!r}"


def test_classifier_handles_env_var_prefix():
    """`FOO=bar terraform destroy` should still classify as terraform."""
    intent = classify("AWS_REGION=us-east-1 terraform destroy")
    assert intent.is_infra_command
    assert intent.operation == "terraform destroy"


def test_classifier_handles_sudo():
    intent = classify("sudo kubectl delete deployment payments")
    assert intent.is_infra_command
    assert intent.target == "payments"


def test_classifier_picks_first_destructive_segment():
    """Pipes / && — surface the first infra-affecting segment."""
    intent = classify("echo hi && terraform destroy")
    assert intent.is_infra_command
    assert intent.operation == "terraform destroy"


def test_classifier_quoted_args():
    intent = classify('aws s3 rb "s3://my bucket" --force')
    assert intent.is_infra_command


# ---------------------------------------------------------------------------
# End-to-end hook entrypoint
# ---------------------------------------------------------------------------

def _run_hook_with_stdin(payload: dict, *, db_path=None,
                        env: dict | None = None) -> tuple[int, str]:
    """Helper: feed the hook a JSON payload via stdin, capture stderr + exit."""
    import engram.hook.main as hookmain

    stdin_text = json.dumps(payload)
    stderr_buf = io.StringIO()

    old_env = os.environ.copy()
    try:
        if env:
            os.environ.update(env)
        if db_path is not None:
            os.environ["ENGRAM_DATA_DIR"] = str(db_path)

        with patch.object(sys, "stdin", io.StringIO(stdin_text)), \
             redirect_stderr(stderr_buf):
            rc = hookmain.run()
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    return rc, stderr_buf.getvalue()


def test_hook_allows_non_bash_tool_calls(tmp_path):
    """Read tool, file edit tool — anything not Bash → exit 0 immediately."""
    payload = {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}
    rc, _ = _run_hook_with_stdin(payload, db_path=tmp_path)
    assert rc == 0


def test_hook_allows_non_infra_bash(tmp_path):
    """ls / cat / git — all allowed."""
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"}}
    rc, _ = _run_hook_with_stdin(payload, db_path=tmp_path)
    assert rc == 0


def test_hook_allows_when_db_missing(tmp_path):
    """No DB at the configured path → allow but warn on stderr."""
    payload = {"tool_name": "Bash", "tool_input": {"command": "terraform destroy"}}
    nonexistent = tmp_path / "nope"
    rc, err = _run_hook_with_stdin(payload, db_path=nonexistent)
    assert rc == 0
    assert "DB not initialised" in err or "Allowing" in err


def test_hook_blocks_destructive_on_prod_resource(tmp_db, tmp_path):
    """Seed a prod RDS, run the hook with `terraform destroy <name>` →
    exit 2 (BLOCK)."""
    from engram.config import Config
    from engram.db import open_db
    from engram.graph import (
        FileRow, ProjectRow, ResourceRow,
        resource_uid, upsert_file, upsert_project, upsert_resource,
    )

    # Build the DB at a known location.
    db_dir = tmp_path / "data"
    db_dir.mkdir(exist_ok=True)
    cfg = Config(data_dir=db_dir, log_dir=tmp_path / "logs",
                 watch_paths=[], embeddings_enabled=False)
    cfg.ensure_dirs()
    conn = open_db(cfg)

    upsert_project(conn, ProjectRow(path="/r", name="r", project_type="terraform"))
    upsert_file(conn, FileRow(
        path="/r/main.tf", project_path="/r", name="main.tf",
        extension=".tf", size_bytes=100, content_hash="h",
        modified_at="2026-05-16T00:00:00Z", risk_tier="red",
    ))
    upsert_resource(conn, ResourceRow(
        uid=resource_uid("tf:aws_db_instance", "payments-prod", "", "/r/main.tf"),
        file_path="/r/main.tf", kind="tf:aws_db_instance",
        name="payments-prod", environment="production", risk_tier="red",
    ))
    conn.close()

    payload = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "terraform destroy -target=aws_db_instance.payments-prod",
        },
    }
    rc, err = _run_hook_with_stdin(payload, db_path=db_dir)
    assert rc == 2, f"expected BLOCK (rc=2), got rc={rc}, stderr={err}"
    assert "BLOCK" in err
    assert "PRODUCTION" in err.upper()


def test_hook_confirms_destructive_on_unknown_target(tmp_db, tmp_path):
    """Destructive op on unknown target → exit 1 (CONFIRM)."""
    from engram.config import Config
    from engram.db import open_db

    db_dir = tmp_path / "data"
    db_dir.mkdir(exist_ok=True)
    cfg = Config(data_dir=db_dir, log_dir=tmp_path / "logs",
                 watch_paths=[], embeddings_enabled=False)
    cfg.ensure_dirs()
    open_db(cfg).close()

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "kubectl delete deployment unknown-svc"},
    }
    rc, _ = _run_hook_with_stdin(payload, db_path=db_dir)
    assert rc == 1


def test_hook_allows_read_op(tmp_path):
    """`kubectl get pods` → exit 0."""
    from engram.config import Config
    from engram.db import open_db

    db_dir = tmp_path / "data"
    db_dir.mkdir(exist_ok=True)
    cfg = Config(data_dir=db_dir, log_dir=tmp_path / "logs",
                 watch_paths=[], embeddings_enabled=False)
    cfg.ensure_dirs()
    open_db(cfg).close()

    payload = {"tool_name": "Bash", "tool_input": {"command": "kubectl get pods"}}
    rc, _ = _run_hook_with_stdin(payload, db_path=db_dir)
    assert rc == 0


def test_hook_override_allows_red(tmp_path):
    """ENGRAM_HOOK_ALLOW=red turns BLOCK into ALLOW."""
    from engram.config import Config
    from engram.db import open_db
    from engram.graph import (
        FileRow, ProjectRow, ResourceRow,
        resource_uid, upsert_file, upsert_project, upsert_resource,
    )

    db_dir = tmp_path / "data"
    db_dir.mkdir(exist_ok=True)
    cfg = Config(data_dir=db_dir, log_dir=tmp_path / "logs",
                 watch_paths=[], embeddings_enabled=False)
    cfg.ensure_dirs()
    conn = open_db(cfg)
    upsert_project(conn, ProjectRow(path="/r", name="r"))
    upsert_file(conn, FileRow(
        path="/r/main.tf", project_path="/r", name="main.tf",
        extension=".tf", size_bytes=100, content_hash="h",
        modified_at="2026-05-16T00:00:00Z", risk_tier="red",
    ))
    upsert_resource(conn, ResourceRow(
        uid=resource_uid("tf:aws_db_instance", "prod-db", "", "/r/main.tf"),
        file_path="/r/main.tf", kind="tf:aws_db_instance",
        name="prod-db", environment="production", risk_tier="red",
    ))
    conn.close()

    payload = {"tool_name": "Bash",
               "tool_input": {"command": "terraform destroy -target=aws_db_instance.prod-db"}}
    # Without override → blocked.
    rc1, _ = _run_hook_with_stdin(payload, db_path=db_dir)
    assert rc1 == 2

    # With override → allowed, and an audit row written.
    rc2, _ = _run_hook_with_stdin(
        payload, db_path=db_dir, env={"ENGRAM_HOOK_ALLOW": "red"},
    )
    assert rc2 == 0

    # Verify the audit row exists.
    conn = open_db(cfg)
    n = conn.execute(
        "SELECT count(*) FROM memory WHERE source='hook-override'"
    ).fetchone()[0]
    assert n >= 1


def test_hook_bad_json_does_not_crash(tmp_path):
    """Malformed JSON → default-allow (not fail-closed) unless STRICT."""
    import engram.hook.main as hookmain

    with patch.object(sys, "stdin", io.StringIO("not json {{{")), \
         redirect_stderr(io.StringIO()):
        rc = hookmain.run()
    assert rc == 0  # default-allow

    # In strict mode, same payload → block.
    old = os.environ.copy()
    try:
        os.environ["ENGRAM_HOOK_STRICT"] = "1"
        with patch.object(sys, "stdin", io.StringIO("not json {{{")), \
             redirect_stderr(io.StringIO()):
            rc = hookmain.run()
        assert rc == 2
    finally:
        os.environ.clear()
        os.environ.update(old)


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------

def test_installer_creates_new_settings(tmp_path, monkeypatch):
    """install_hook on a missing settings.json creates the file from scratch."""
    monkeypatch.setattr(
        "engram.hook.installer._claude_settings_path",
        lambda: tmp_path / "settings.json",
    )
    from engram.hook.installer import install_hook
    result = install_hook("claude-code", dry_run=False)
    assert result["action"] == "created"
    config = json.loads((tmp_path / "settings.json").read_text())
    assert "hooks" in config
    pre = config["hooks"]["PreToolUse"]
    assert len(pre) == 1
    assert pre[0]["matcher"] == "Bash"
    assert any("engram" in h["command"] and "hook" in h["command"]
               for h in pre[0]["hooks"])


def test_installer_preserves_existing_hooks(tmp_path, monkeypatch):
    """Existing PreToolUse entries from other tools must not be touched."""
    monkeypatch.setattr(
        "engram.hook.installer._claude_settings_path",
        lambda: tmp_path / "settings.json",
    )
    # Pre-existing config with a non-engram Bash hook and a Write matcher.
    pre_existing = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash",
                 "hooks": [{"type": "command", "command": "/usr/local/bin/some-other-hook"}]},
                {"matcher": "Write",
                 "hooks": [{"type": "command", "command": "/path/to/lint"}]},
            ]
        }
    }
    (tmp_path / "settings.json").write_text(json.dumps(pre_existing))

    from engram.hook.installer import install_hook
    result = install_hook("claude-code", dry_run=False)
    assert result["action"] == "created"

    config = json.loads((tmp_path / "settings.json").read_text())
    bash_matcher = next(m for m in config["hooks"]["PreToolUse"]
                        if m["matcher"] == "Bash")
    # Both hooks must be present.
    commands = [h["command"] for h in bash_matcher["hooks"]]
    assert any("some-other-hook" in c for c in commands)
    assert any("engram" in c and "hook" in c for c in commands)
    # Write matcher untouched.
    assert any(m["matcher"] == "Write" for m in config["hooks"]["PreToolUse"])


def test_installer_idempotent(tmp_path, monkeypatch):
    """Running install twice does not duplicate the entry."""
    monkeypatch.setattr(
        "engram.hook.installer._claude_settings_path",
        lambda: tmp_path / "settings.json",
    )
    from engram.hook.installer import install_hook
    install_hook("claude-code", dry_run=False)
    second = install_hook("claude-code", dry_run=False)
    assert second["action"] == "unchanged"
    # Only one engram entry.
    config = json.loads((tmp_path / "settings.json").read_text())
    bash_matcher = next(m for m in config["hooks"]["PreToolUse"]
                        if m["matcher"] == "Bash")
    engram_entries = [h for h in bash_matcher["hooks"]
                      if isinstance(h.get("command"), str)
                      and "engram" in h["command"] and "hook" in h["command"]]
    assert len(engram_entries) == 1


def test_installer_uninstall(tmp_path, monkeypatch):
    """Uninstall removes our hook, leaves others in place."""
    monkeypatch.setattr(
        "engram.hook.installer._claude_settings_path",
        lambda: tmp_path / "settings.json",
    )
    from engram.hook.installer import install_hook, uninstall_hook

    pre_existing = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash",
                 "hooks": [{"type": "command", "command": "/usr/local/bin/some-other-hook"}]},
            ]
        }
    }
    (tmp_path / "settings.json").write_text(json.dumps(pre_existing))

    install_hook("claude-code", dry_run=False)
    result = uninstall_hook("claude-code")
    assert result["action"] == "removed"

    config = json.loads((tmp_path / "settings.json").read_text())
    bash_matcher = next(m for m in config["hooks"]["PreToolUse"]
                        if m["matcher"] == "Bash")
    commands = [h["command"] for h in bash_matcher["hooks"]]
    assert any("some-other-hook" in c for c in commands)
    assert not any("engram" in c and "hook" in c for c in commands)
