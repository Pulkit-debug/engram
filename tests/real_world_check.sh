#!/usr/bin/env bash
# Run the full Engram pipeline against an indexed real-world repo and emit
# a structured validation report.
#
# Usage: real_world_check.sh <repo_clone_path> <db_dir>

set -e
REPO="$1"
DB_DIR="$2"
ENGRAM=$(realpath "$(dirname "$0")/../.venv/bin/engram")

export ENGRAM_DATA_DIR="$DB_DIR"

echo "============================================================"
echo "  Engram real-world validation: $(basename "$REPO")"
echo "============================================================"

echo
echo "## Status"
"$ENGRAM" status 2>&1 | tail -12

echo
echo "## Top 10 k8s resources"
"$(dirname "$ENGRAM")/python3" -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); [print(*r,sep='  ') for r in c.execute(sys.argv[2])]" "$DB_DIR/engram.db" \
  "SELECT kind || '  ' || name || '  ' || coalesce(nullif(environment,''),'-') || '  ' || risk_tier
   FROM resource WHERE kind LIKE 'k8s:%' ORDER BY risk_tier DESC, kind LIMIT 10;"

echo
echo "## Helm charts found"
"$(dirname "$ENGRAM")/python3" -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); [print(*r,sep='  ') for r in c.execute(sys.argv[2])]" "$DB_DIR/engram.db" \
  "SELECT kind || '  ' || name FROM resource WHERE kind LIKE 'helm:%' ORDER BY name;"

echo
echo "## Cross-format service names (>= 2 formats)"
"$(dirname "$ENGRAM")/python3" -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); [print(*r,sep='  ') for r in c.execute(sys.argv[2])]" "$DB_DIR/engram.db" \
  "SELECT name, count(DISTINCT kind) AS formats, group_concat(DISTINCT kind)
   FROM resource
   GROUP BY name HAVING formats >= 2
   ORDER BY formats DESC LIMIT 10;"

echo
echo "## Tech inventory"
"$(dirname "$ENGRAM")/python3" -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); [print(*r,sep='  ') for r in c.execute(sys.argv[2])]" "$DB_DIR/engram.db" \
  "SELECT name FROM technology ORDER BY name;"

echo
echo "## Assess scenarios"
for op_target in \
    "terraform destroy|server" \
    "kubectl delete deployment|cert-manager" \
    "helm uninstall|vault" \
    "kubectl apply|webhook" \
    "kubectl get|cainjector"
do
    OP="${op_target%%|*}"
    TGT="${op_target##*|}"
    echo
    echo "### assess '$OP' '$TGT'"
    "$ENGRAM" assess "$OP" "$TGT" 2>&1 | head -10
done

echo
echo "## Emit AGENTS.md"
EMIT_TARGET="$DB_DIR/AGENTS.md"
"$ENGRAM" emit-agents-md --target "$EMIT_TARGET" 2>&1 | head -3
echo "  written: $EMIT_TARGET ($(wc -l < "$EMIT_TARGET") lines)"
echo
echo "## Check-drift (must be 'up to date' right after emit)"
"$ENGRAM" check-drift --target "$EMIT_TARGET" 2>&1 | head -3
