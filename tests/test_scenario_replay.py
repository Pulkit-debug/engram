"""Pytest harness for the per-repo scenario YAMLs.

Each scenario asserts that `engram.safety.blast_radius.assess(...)` returns
the expected (tier, action) on a *real indexed graph* of an OSS DevOps repo.

These tests are *skipped* if the bench DBs at /tmp/engram_bench_out aren't
present. The benchmark script (`tests/benchmark_real_repos.sh`) populates
them; CI can either run that script first or skip these tests.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from engram.safety.blast_radius import assess


_BENCH_ROOT = Path("/tmp/engram_bench_out")
_SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def _scenario_files() -> list[tuple[str, Path]]:
    if not _SCENARIOS_DIR.exists():
        return []
    return [(p.stem, p) for p in sorted(_SCENARIOS_DIR.glob("*.yaml"))]


def _expand_cases():
    """Flatten every scenario in every YAML into a list of (id, alias, scenario_dict)."""
    out: list[tuple[str, str, dict]] = []
    for alias, scen_file in _scenario_files():
        scenarios = yaml.safe_load(scen_file.read_text(encoding="utf-8")) or []
        for scen in scenarios:
            out.append((f"{alias}::{scen['name']}", alias, scen))
    return out


@pytest.mark.parametrize(
    "scen_id,alias,scen",
    _expand_cases(),
    ids=[c[0] for c in _expand_cases()],
)
def test_scenario(scen_id: str, alias: str, scen: dict) -> None:
    db_path = _BENCH_ROOT / alias / "engram.db"
    if not db_path.exists():
        pytest.skip(
            f"Bench DB missing at {db_path}. Run tests/benchmark_real_repos.sh first."
        )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        result = assess(conn, scen["operation"], scen["target"])

        if "expected_tier" in scen:
            assert result.risk_tier == scen["expected_tier"], (
                f"tier mismatch for {scen_id}: "
                f"got {result.risk_tier!r}, expected {scen['expected_tier']!r}"
            )
        if "expected_action" in scen:
            assert result.action == scen["expected_action"], (
                f"action mismatch for {scen_id}: "
                f"got {result.action!r}, expected {scen['expected_action']!r}"
            )
        if "min_resources" in scen:
            assert len(result.resolved_resources) >= scen["min_resources"], (
                f"resources mismatch for {scen_id}: "
                f"got {len(result.resolved_resources)}, expected >= {scen['min_resources']}"
            )
        if "max_resources" in scen:
            assert len(result.resolved_resources) <= scen["max_resources"], (
                f"resources mismatch for {scen_id}: "
                f"got {len(result.resolved_resources)}, expected <= {scen['max_resources']}"
            )
        if "min_dependents" in scen:
            assert len(result.dependents) >= scen["min_dependents"], (
                f"dependents mismatch for {scen_id}: "
                f"got {len(result.dependents)}, expected >= {scen['min_dependents']}"
            )
    finally:
        conn.close()
