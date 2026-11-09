from __future__ import annotations

from pathlib import Path

from scripts.ci_gate import evaluate_ci_gate
from scripts.ci_test_shards import (
    POSTGRES_EXPRESSION,
    PREFLIGHT_TEST_FILES,
    collect_node_id_list,
    collect_node_ids,
    discover_test_files,
    get_shards,
    get_shard_selectors,
    verify_collection_coverage,
)


def test_recursive_discovery_and_deterministic_partition(tmp_path: Path):
    nested = tmp_path / "tests" / "nested"
    nested.mkdir(parents=True)
    (tmp_path / "tests" / "test_root.py").write_text("def test_root(): pass\n", encoding="utf-8")
    (nested / "test_nested.py").write_text("def test_nested(): pass\n", encoding="utf-8")

    files = discover_test_files(str(tmp_path / "tests"))
    assert files == [
        (tmp_path / "tests" / "nested" / "test_nested.py").as_posix(),
        (tmp_path / "tests" / "test_root.py").as_posix(),
    ]
    shards = get_shards(3, files)
    assert sorted(path for shard in shards for path in shard) == files
    assert sum(len(shard) for shard in shards) == 2


def test_current_collection_is_partitioned_without_overlap():
    ok, message = verify_collection_coverage(3)
    assert ok, message


def test_preflight_tests_are_not_assigned_to_regression_shards():
    all_sqlite_nodes = collect_node_id_list(marker=f"not {POSTGRES_EXPRESSION}")
    preflight_nodes = collect_node_ids(
        marker=f"not {POSTGRES_EXPRESSION}",
        files=list(PREFLIGHT_TEST_FILES),
    )
    regression_nodes = [node for node in all_sqlite_nodes if node not in preflight_nodes]
    shards = get_shard_selectors(3, regression_nodes, all_sqlite_nodes=all_sqlite_nodes)
    assigned = set().union(
        *(collect_node_ids(marker=f"not {POSTGRES_EXPRESSION}", files=shard) for shard in shards)
    )
    assert assigned.isdisjoint(preflight_nodes)
    assert assigned == set(regression_nodes)


def test_ci_gate_requires_all_full_checks():
    assert evaluate_ci_gate("full", "skipped", "success", "success", "success")[0]
    assert not evaluate_ci_gate("full", "skipped", "success", "success", "skipped")[0]
    assert evaluate_ci_gate("draft", "success", "skipped", "skipped", "skipped")[0]
    assert not evaluate_ci_gate("draft", "success", "skipped", "success", "skipped")[0]
