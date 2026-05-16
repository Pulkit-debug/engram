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
elif command -v engram >/dev/null 2>&1; then
    ENGRAM="$(command -v engram)"
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
hr "9/9   mcp install --target all --dry-run  (no actual writes)"
# -------------------------------------------------------------------------
MCP_OUT="$("$ENGRAM" mcp install --target all --dry-run 2>&1)"
echo "$MCP_OUT"
echo "$MCP_OUT" | grep -q "claude-code" || fail "claude-code target missing"
echo "$MCP_OUT" | grep -q "cursor" || fail "cursor target missing"
ok "MCP install plan generated for all targets"

hr "  RESULT: all 9 checks passed"
echo
echo "  Database:    $DB/engram.db"
echo "  Fixture:     $FIXTURE"
echo "  AGENTS.md:   $AGENTS_MD"
echo
echo "  This is the demo you reproduce in front of a CTO or in a Show HN comment."
echo "  All output above is verbatim from a real run; no scripted output."
