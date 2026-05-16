"""Replay scenario YAMLs against an indexed DB and report pass/fail.

Each scenario row is a dict like:

  - name: "destroy_postgres"
    operation: "kubectl delete deployment"
    target: "postgres"
    expected_tier: "orange"          # green|orange|red
    expected_action: "confirm"        # proceed|confirm|block
    min_resources: 2                  # optional: at least N resolved
    max_resources: 0                  # optional: at most N (use 0 for "none")
    min_dependents: 1                 # optional
    rationale: "free text"

Usage: python tests/_scenario_replay.py <db_path> <scenarios.yaml>
       exits 0 if all pass, 1 if any fail.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import yaml

from engram.safety.blast_radius import assess


def main() -> int:
    db_path = sys.argv[1]
    scenarios_path = Path(sys.argv[2])
    scenarios = yaml.safe_load(scenarios_path.read_text(encoding="utf-8"))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    passed, failed = 0, 0
    for scen in scenarios:
        name = scen["name"]
        result = assess(conn, scen["operation"], scen["target"])

        errors: list[str] = []
        if "expected_tier" in scen and result.risk_tier != scen["expected_tier"]:
            errors.append(f"tier {result.risk_tier} != {scen['expected_tier']}")
        if "expected_action" in scen and result.action != scen["expected_action"]:
            errors.append(f"action {result.action} != {scen['expected_action']}")
        if "min_resources" in scen and len(result.resolved_resources) < scen["min_resources"]:
            errors.append(f"resources {len(result.resolved_resources)} < {scen['min_resources']}")
        if "max_resources" in scen and len(result.resolved_resources) > scen["max_resources"]:
            errors.append(f"resources {len(result.resolved_resources)} > {scen['max_resources']}")
        if "min_dependents" in scen and len(result.dependents) < scen["min_dependents"]:
            errors.append(f"dependents {len(result.dependents)} < {scen['min_dependents']}")

        if errors:
            failed += 1
            print(f"    ✗ {name}")
            for e in errors:
                print(f"        {e}")
            print(f"        got: tier={result.risk_tier}, action={result.action}, "
                  f"resources={len(result.resolved_resources)}, dependents={len(result.dependents)}")
        else:
            passed += 1
            print(f"    ✓ {name}  (tier={result.risk_tier}, action={result.action}, "
                  f"resources={len(result.resolved_resources)})")

    total = passed + failed
    print(f"\n    Score: {passed}/{total} scenarios passed")
    conn.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
