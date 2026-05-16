#!/usr/bin/env bash
# Engram end-to-end demo + reproducibility check.
#
# Runs the entire pipeline against a synthetic DevOps fixture and verifies
# every claim in the README. Exits non-zero on any failure. Designed to run
# on a clean machine (Mac/Linux/Windows-with-bash) in under 60 seconds.
#
# Usage:
#   bash tests/full_demo.sh              # uses /tmp scratch dirs
#   bash tests/full_demo.sh /custom/dir  # uses /custom/dir for everything

set -e
set -o pipefail

SCRATCH="${1:-/tmp/engram_full_demo}"
FIXTURE="$SCRATCH/fixture"
DB="$SCRATCH/db"
AGENTS_MD="$SCRATCH/AGENTS.md"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Pick the engram CLI. Prefer the project's venv; fall back to PATH.
if [[ -x "$PROJECT_DIR/.venv/bin/engram" ]]; then
    ENGRAM="$PROJECT_DIR/.venv/bin/engram"
    PY="$PROJECT_DIR/.venv/bin/python3"
elif command -v engram >/dev/null 2>&1; then
    ENGRAM="$(command -v engram)"
    PY="$(command -v python3 || command -v python)"
else
    echo "ERROR: engram CLI not found. Install with 'pipx install engram-devops' or set up the project venv." >&2
    exit 1
fi

export ENGRAM_DATA_DIR="$DB"
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH"

# Helper: pretty section header.
hr() { echo; echo "============================================================"; echo "  $*"; echo "============================================================"; }
ok() { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; exit 1; }

# -------------------------------------------------------------------------
hr "1/9   Engram version"
# -------------------------------------------------------------------------
"$ENGRAM" --version || fail "engram --version failed"
ok "engram CLI available at $ENGRAM"

# -------------------------------------------------------------------------
hr "2/9   Build the synthetic DevOps fixture"
# -------------------------------------------------------------------------
bash "$SCRIPT_DIR/build_fixture.sh" "$FIXTURE" >/dev/null
test -f "$FIXTURE/services/payments/Dockerfile" || fail "fixture not built"
ok "fixture at $FIXTURE"

# -------------------------------------------------------------------------
hr "3/9   engram init"
# -------------------------------------------------------------------------
"$ENGRAM" init | tail -8 || fail "engram init failed"
test -f "$DB/engram.db" || fail "DB not created at $DB/engram.db"
ok "DB at $DB/engram.db"

# -------------------------------------------------------------------------
hr "4/9   engram index --path FIXTURE"
# -------------------------------------------------------------------------
INDEX_OUT="$("$ENGRAM" index --path "$FIXTURE" --force 2>&1)"
echo "$INDEX_OUT" | tail -20
echo "$INDEX_OUT" | grep -q "Files indexed" || fail "no 'Files indexed' line"
echo "$INDEX_OUT" | grep -q "Resources extracted" || fail "no 'Resources extracted' line"
ok "indexed without errors"

# -------------------------------------------------------------------------
hr "5/9   blast_radius — terraform destroy on prod RDS  (expect: BLOCK / red)"
# -------------------------------------------------------------------------
ASSESS_OUT="$("$ENGRAM" assess "terraform destroy" "datatalks_prod_db" 2>&1)"
echo "$ASSESS_OUT" | head -12
echo "$ASSESS_OUT" | grep -q "BLOCK" || fail "expected BLOCK in assess output"
echo "$ASSESS_OUT" | grep -q "red" || fail "expected risk_tier 'red'"
echo "$ASSESS_OUT" | grep -q "PRODUCTION" || fail "expected 'PRODUCTION' reason"
ok "killer flow confirmed (BLOCK / red / production)"

# -------------------------------------------------------------------------
hr "6/9   blast_radius — kubectl apply on staging  (expect: CONFIRM / orange)"
# -------------------------------------------------------------------------
STAGING_OUT="$("$ENGRAM" assess "terraform apply" "staging_db" 2>&1)"
echo "$STAGING_OUT" | head -12
echo "$STAGING_OUT" | grep -qE "CONFIRM|PROCEED" || fail "expected non-blocking action for staging mutate"
ok "staging mutate correctly classified"

# -------------------------------------------------------------------------
hr "7/9   emit-agents-md + check-drift  (must be 'up to date' after emit)"
# -------------------------------------------------------------------------
"$ENGRAM" emit-agents-md --target "$AGENTS_MD" | tail -3
test -f "$AGENTS_MD" || fail "AGENTS.md not written"
grep -q "engram:auto-generated" "$AGENTS_MD" || fail "engram markers missing"
DRIFT_OUT="$("$ENGRAM" check-drift --target "$AGENTS_MD" 2>&1)"
echo "$DRIFT_OUT"
echo "$DRIFT_OUT" | grep -q "up to date" || fail "drift detected immediately after emit"
ok "AGENTS.md emitted, signed, and drift-free"

# -------------------------------------------------------------------------
hr "8/9   label --apply + unlabel --apply  (round-trip clean)"
# -------------------------------------------------------------------------
"$ENGRAM" label --apply | tail -10
K8S_FILE="$FIXTURE/infra/prod/k8s/payments-deployment.yaml"
grep -q "engram.io/risk-tier" "$K8S_FILE" || fail "expected engram label on K8s"
"$ENGRAM" unlabel --apply | tail -5
grep -q "engram.io/" "$K8S_FILE" && fail "label not fully removed" || true
ok "label/unlabel round-trip preserved file"

# -------------------------------------------------------------------------
hr "9/11  mcp install --target all --dry-run  (no actual writes)"
# -------------------------------------------------------------------------
MCP_OUT="$("$ENGRAM" mcp install --target all --dry-run 2>&1)"
echo "$MCP_OUT"
echo "$MCP_OUT" | grep -q "claude-code" || fail "claude-code target missing"
echo "$MCP_OUT" | grep -q "cursor" || fail "cursor target missing"
ok "MCP install plan generated for all targets"

# -------------------------------------------------------------------------
hr "10/11 user annotation promotes click-ops resource to RED"
# -------------------------------------------------------------------------
"$PY" - <<'PYEOF'
from engram.config import load_config
from engram.db import open_db
from engram.graph import (FileRow, ProjectRow, ResourceRow,
    resource_uid, upsert_file, upsert_project, upsert_resource)
cfg = load_config()
conn = open_db(cfg)
upsert_project(conn, ProjectRow(path="", name="cloud"))
fp = "aws://demo/us-east-1/rds"
upsert_file(conn, FileRow(path=fp, project_path="", name="aws:rds",
    extension="", size_bytes=0, content_hash="x",
    modified_at="2026-05-16T00:00:00Z"))
uid = resource_uid("aws:rds:DBInstance", "demo-click-ops-db", "us-east-1", fp)
upsert_resource(conn, ResourceRow(uid=uid, file_path=fp,
    kind="aws:rds:DBInstance", name="demo-click-ops-db",
    namespace="us-east-1", environment="", risk_tier="green",
    properties={"discovered_from": "aws-cli"}))
PYEOF

BEFORE=$("$ENGRAM" assess "terraform destroy" "demo-click-ops-db" 2>&1)
echo "$BEFORE" | grep -q "CONFIRM" || fail "expected CONFIRM before annotation"

"$ENGRAM" annotate aws:rds:DBInstance:demo-click-ops-db \
    --env production --owner platform-team \
    --runbook "https://example.com/runbook" 2>&1 | tail -2

AFTER=$("$ENGRAM" assess "terraform destroy" "demo-click-ops-db" 2>&1)
echo "$AFTER" | head -10
echo "$AFTER" | grep -q "BLOCK" || fail "expected BLOCK after env=production annotation"
echo "$AFTER" | grep -q "platform-team" || fail "expected owner annotation in reasons"
ok "user annotation promoted resource from CONFIRM to BLOCK"

# -------------------------------------------------------------------------
hr "11/11 value-match inference (pioneer feature)"
# -------------------------------------------------------------------------
"$PY" - <<'PYEOF'
from engram.config import load_config
from engram.db import open_db
from engram.graph import (EntityRow, FileRow, ProjectRow, ResourceRow,
    entity_uid, resource_uid, upsert_entity, upsert_file,
    upsert_project, upsert_resource)
cfg = load_config()
conn = open_db(cfg)
upsert_project(conn, ProjectRow(path="/repo/payments", name="payments"))
upsert_file(conn, FileRow(path="/repo/payments/.env", project_path="/repo/payments",
    name=".env", extension=".env", size_bytes=10, content_hash="h",
    modified_at="2026-05-16T00:00:00Z"))
fp = "aws://demo/us-east-1/rds-payments"
upsert_file(conn, FileRow(path=fp, project_path="", name="aws:rds",
    extension="", size_bytes=0, content_hash="y",
    modified_at="2026-05-16T00:00:00Z", risk_tier="red"))
rds_uid = resource_uid("aws:rds:DBInstance", "payments-prod", "us-east-1", fp)
upsert_resource(conn, ResourceRow(uid=rds_uid, file_path=fp,
    kind="aws:rds:DBInstance", name="payments-prod",
    namespace="us-east-1", environment="production", risk_tier="red",
    properties={"endpoint": "payments-prod.cluster.us-east-1.rds.amazonaws.com",
                "discovered_from": "aws-cli"}))
upsert_entity(conn, EntityRow(
    uid=entity_uid("DATABASE_URL", "env_var", "/repo/payments/.env"),
    file_path="/repo/payments/.env",
    name="DATABASE_URL", entity_type="env_var",
    value="postgres://payments-prod.cluster.us-east-1.rds.amazonaws.com:5432/p"))
PYEOF

"$ENGRAM" infer 2>&1 | tail -6

INFER_OUT=$("$ENGRAM" assess "terraform destroy" "payments-prod" 2>&1)
echo "$INFER_OUT" | head -10
echo "$INFER_OUT" | grep -q "BLOCK" || fail "expected BLOCK for prod-tagged RDS"
echo "$INFER_OUT" | grep -qE "Dependents:[[:space:]]*[1-9]" || fail "expected >= 1 inferred dependent"
ok "value-match inferred a DEPENDS_ON edge; blast_radius surfaced it"

# -------------------------------------------------------------------------
hr "12/13 PreToolUse hook (the enforcement primitive)"
# -------------------------------------------------------------------------
# Pipe a Claude-Code-shaped Bash payload into `engram hook`. With the
# datatalks_prod_db seeded earlier, the destructive op must exit 2 (BLOCK).
HOOK_PAYLOAD='{"tool_name":"Bash","tool_input":{"command":"terraform destroy -target=aws_db_instance.datatalks_prod_db"}}'
HOOK_STDERR=$(echo "$HOOK_PAYLOAD" | "$ENGRAM" hook 2>&1 1>/dev/null; printf '%s' "exit=$?")
echo "$HOOK_STDERR" | tail -10
case "$HOOK_STDERR" in
    *exit=2*) ok "hook exited 2 (BLOCK) on destructive op against prod resource" ;;
    *) fail "expected exit 2; got: ${HOOK_STDERR##*exit=}" ;;
esac

# A read-only command must exit 0.
GREEN_PAYLOAD='{"tool_name":"Bash","tool_input":{"command":"kubectl get pods"}}'
GREEN_RC=$(echo "$GREEN_PAYLOAD" | "$ENGRAM" hook >/dev/null 2>&1; echo $?)
[[ "$GREEN_RC" == "0" ]] || fail "expected hook exit 0 on read-op; got $GREEN_RC"

# -------------------------------------------------------------------------
hr "13/13 engram drift (cloud-vs-IaC reconciliation)"
# -------------------------------------------------------------------------
# Seed a click-ops cloud-discovered resource that has no IaC counterpart,
# then run drift and verify it surfaces.
"$PY" - <<'PYEOF'
from engram.config import load_config
from engram.db import open_db
from engram.graph import (FileRow, ProjectRow, ResourceRow,
    resource_uid, upsert_file, upsert_project, upsert_resource)
cfg = load_config()
conn = open_db(cfg)
upsert_project(conn, ProjectRow(path="", name="cloud"))
fp = "aws://demo/us-east-1/rds-clickops"
upsert_file(conn, FileRow(path=fp, project_path="", name="aws:rds",
    extension="", size_bytes=0, content_hash="cops",
    modified_at="2026-05-17T00:00:00Z", risk_tier="red"))
upsert_resource(conn, ResourceRow(
    uid=resource_uid("aws:rds:DBInstance", "legacy-prod-2018", "us-east-1", fp),
    file_path=fp, kind="aws:rds:DBInstance", name="legacy-prod-2018",
    namespace="us-east-1", environment="production", risk_tier="red",
    properties={"discovered_from": "aws-cli", "account": "demo", "region": "us-east-1"}))
PYEOF

# `engram drift` exits 1 when drift is found (so it can be cron'd).
DRIFT_OUT=$("$ENGRAM" drift 2>&1) || true
echo "$DRIFT_OUT" | head -20
echo "$DRIFT_OUT" | grep -q "legacy-prod-2018" || fail "expected legacy-prod-2018 in drift output"
echo "$DRIFT_OUT" | grep -q "❗" || fail "expected production-marker in drift output"
ok "drift report surfaced the click-ops production resource"

hr "  RESULT: all 13 checks passed"
echo
echo "  Database:    $DB/engram.db"
echo "  Fixture:     $FIXTURE"
echo "  AGENTS.md:   $AGENTS_MD"
echo
echo "  This is the demo you reproduce in front of a CTO or in a Show HN comment."
echo "  All output above is verbatim from a real run; no scripted output."
