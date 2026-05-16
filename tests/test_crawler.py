"""End-to-end crawler test."""

from engram.crawler import index_paths
from engram.graph import graph_stats


def test_crawler_indexes_fixture(tmp_db, tmp_cfg, fixture_repo):
    tmp_cfg.watch_paths = [fixture_repo]
    stats = index_paths(tmp_db, tmp_cfg)
    assert stats.files_indexed >= 3
    assert stats.resources_extracted >= 2
    assert "python" in stats.technologies
    assert "terraform" in stats.technologies or "aws" in stats.technologies


def test_crawler_skips_unchanged(tmp_db, tmp_cfg, fixture_repo):
    tmp_cfg.watch_paths = [fixture_repo]
    first = index_paths(tmp_db, tmp_cfg)
    # Second run with no changes should skip everything.
    second = index_paths(tmp_db, tmp_cfg)
    assert second.files_indexed == 0
    assert second.files_skipped >= first.files_indexed


def test_crawler_populates_graph(tmp_db, tmp_cfg, fixture_repo):
    tmp_cfg.watch_paths = [fixture_repo]
    index_paths(tmp_db, tmp_cfg)
    counts = graph_stats(tmp_db)
    assert counts["file"] >= 3
    assert counts["resource"] >= 2
    assert counts["technology"] >= 2
    assert counts["edge"] >= 1
