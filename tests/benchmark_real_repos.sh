#!/usr/bin/env bash
# Engram real-repo benchmark harness.
#
# For each of 6 deliberately-different OSS DevOps repos, this script:
#   1. shallow-clones (if not already present)
#   2. indexes into its own SQLite DB so per-repo numbers are clean
#   3. captures: file counts, resource counts, edge counts, wall-clock,
#      extractor coverage, top resources, cross-format services
#   4. runs the scenario-replay tests for that repo (if present)
#   5. emits a markdown row for docs/BENCHMARKS.md
#
# Reproducibility script: anyone with the repo + .venv can run this.
#
# Usage:
#   bash tests/benchmark_real_repos.sh            # uses /tmp/engram_real
#   bash tests/benchmark_real_repos.sh /custom    # uses /custom

set -e
set -o pipefail

REAL_ROOT="${1:-/tmp/engram_real}"
BENCH_OUT="${2:-/tmp/engram_bench_out}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENGRAM="$PROJECT_DIR/.venv/bin/engram"
PY="$PROJECT_DIR/.venv/bin/python3"

mkdir -p "$REAL_ROOT" "$BENCH_OUT"

# Repo manifest: alias, git url
REPOS=(
    "vault-helm|https://github.com/hashicorp/vault-helm.git"
    "cert-manager|https://github.com/cert-manager/cert-manager.git"
    "terraform-aws-eks|https://github.com/terraform-aws-modules/terraform-aws-eks.git"
    "microservices-demo|https://github.com/GoogleCloudPlatform/microservices-demo.git"
    "awesome-compose|https://github.com/docker/awesome-compose.git"
    "helm-charts|https://github.com/prometheus-community/helm-charts.git"
)

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

hr() { echo; echo "============================================================"; echo "  $*"; echo "============================================================"; }
ok() { echo "  ✓ $*"; }

clone_if_missing() {
    local alias="$1" url="$2"
    if [[ ! -d "$REAL_ROOT/$alias" ]]; then
        echo "  cloning $alias..." >&2
        git -C "$REAL_ROOT" clone --depth 1 "$url" "$alias" >/dev/null 2>&1
    fi
}

repo_size_mb() {
    du -sm "$1" | awk '{print $1}'
}

# ----------------------------------------------------------------------------
# Per-repo run
# ----------------------------------------------------------------------------

bench_one() {
    local alias="$1"
    local repo_dir="$REAL_ROOT/$alias"
    local db_dir="$BENCH_OUT/$alias"

    hr "$alias  ($(repo_size_mb "$repo_dir")MB)"

    rm -rf "$db_dir"
    mkdir -p "$db_dir"
    export ENGRAM_DATA_DIR="$db_dir"

    "$ENGRAM" init > /dev/null

    # Index with wall-clock measurement.
    local t0=$(date +%s.%N)
    local index_out
    index_out="$("$ENGRAM" index --path "$repo_dir" --force 2>&1)"
    local t1=$(date +%s.%N)
    local elapsed
    elapsed=$(awk "BEGIN { printf \"%.2f\", $t1 - $t0 }")

    # Pull the summary table numbers via the DB directly.
    "$PY" "$SCRIPT_DIR/_bench_dump.py" "$db_dir/engram.db" "$alias" "$elapsed" "$(repo_size_mb "$repo_dir")"

    # Scenario replay (if scenarios file exists for this repo).
    local scen_file="$SCRIPT_DIR/scenarios/$alias.yaml"
    if [[ -f "$scen_file" ]]; then
        echo
        echo "  Scenario replay:"
        "$PY" "$SCRIPT_DIR/_scenario_replay.py" "$db_dir/engram.db" "$scen_file" || true
    fi
}

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

if [[ ! -x "$ENGRAM" ]]; then
    echo "engram CLI missing at $ENGRAM. Run: pip install -e ." >&2
    exit 1
fi

for entry in "${REPOS[@]}"; do
    alias="${entry%%|*}"
    url="${entry##*|}"
    clone_if_missing "$alias" "$url"
done

for entry in "${REPOS[@]}"; do
    alias="${entry%%|*}"
    bench_one "$alias"
done

hr "All 6 repos benchmarked. DBs preserved in $BENCH_OUT for re-querying."
