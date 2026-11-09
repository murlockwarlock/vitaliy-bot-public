#!/usr/bin/env python3
"""Discover, verify, and run isolated CI test partitions."""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path


POSTGRES_MARKER = "postgres_integration"
POSTGRES_EXPRESSION = f"{POSTGRES_MARKER}"
PREFLIGHT_TEST_FILES = (
    "tests/test_startup_router_smoke.py",
    "tests/test_safety_test_mode.py",
)
# These files intentionally stay together because the latter relies on the
# provider state established by the former in the existing regression suite.
COLOCATED_TEST_GROUPS = (
    ("tests/test_openrouter_integration.py", "tests/test_presentation_audit_v2.py"),
)
# The v5 file is large enough to dominate a runner; its tests are isolated
# unittest cases, so splitting its collected nodes keeps the suite balanced.
SPLIT_TEST_FILES = {
    "tests/test_v5_behavioral.py": 2,
}


def discover_test_files(tests_dir: str = "tests") -> list[str]:
    base = Path(tests_dir)
    if not base.exists():
        return []
    return sorted(
        path.as_posix()
        for path in base.rglob("test_*.py")
        if path.is_file()
        and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(base).parts)
    )


def _node_ids(output: str, tests_dir: str = "tests") -> set[str]:
    return set(_node_id_list(output, tests_dir))


def _node_id_list(output: str, tests_dir: str = "tests") -> list[str]:
    prefix = Path(tests_dir).as_posix().rstrip("/") + "/"
    result: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if "::" not in line or not line.startswith(prefix):
            continue
        if line not in result:
            result.append(line)
    return result


def collect_node_ids(
    tests_dir: str = "tests",
    marker: str | None = None,
    files: list[str] | None = None,
    python_exe: str | None = None,
) -> set[str]:
    cmd = [python_exe or sys.executable, "-m", "pytest", "--collect-only", "-q"]
    if marker:
        cmd.extend(["-m", marker])
    cmd.extend(files or [tests_dir])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode not in (0, 5):
        raise RuntimeError(
            f"pytest collection failed with returncode {proc.returncode}:\n{proc.stderr.strip()}"
        )
    return _node_ids(proc.stdout, tests_dir)


def collect_node_id_list(
    tests_dir: str = "tests",
    marker: str | None = None,
    python_exe: str | None = None,
) -> list[str]:
    cmd = [python_exe or sys.executable, "-m", "pytest", "--collect-only", "-q"]
    if marker:
        cmd.extend(["-m", marker])
    cmd.append(tests_dir)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode not in (0, 5):
        raise RuntimeError(
            f"pytest collection failed with returncode {proc.returncode}:\n{proc.stderr.strip()}"
        )
    return _node_id_list(proc.stdout, tests_dir)


def _files_from_nodes(nodes: set[str]) -> set[str]:
    return {node.split("::", 1)[0] for node in nodes}


def get_shards(
    total_shards: int = 3,
    test_files: list[str] | None = None,
) -> list[list[str]]:
    if total_shards <= 0:
        raise ValueError(f"total_shards must be positive, got {total_shards}")

    files = sorted(test_files if test_files is not None else discover_test_files())
    shards: list[list[str]] = [[] for _ in range(total_shards)]
    shard_weights = [0] * total_shards

    # File size is a stable duration proxy for this suite: the largest behavioral
    # files contain the measured slow paths. Greedy placement keeps runner times
    # balanced while remaining deterministic and automatically covering new files.
    file_set = set(files)
    grouped: list[tuple[tuple[str, ...], int]] = []
    grouped_paths: set[str] = set()
    for group in COLOCATED_TEST_GROUPS:
        selected = tuple(path for path in group if path in file_set)
        if selected:
            grouped.append((selected, sum(os.path.getsize(path) for path in selected)))
            grouped_paths.update(selected)
    grouped.extend(((path,), os.path.getsize(path)) for path in files if path not in grouped_paths)
    weighted_files = sorted(
        grouped,
        key=lambda item: (-item[1], item[0]),
    )
    for paths, weight in weighted_files:
        index = min(range(total_shards), key=lambda item: (shard_weights[item], item))
        shards[index].extend(paths)
        shard_weights[index] += weight

    for shard in shards:
        shard.sort()
    flattened = [path for shard in shards for path in shard]
    if set(flattened) != set(files) or len(flattened) != len(set(flattened)):
        raise RuntimeError("Shard partition invariant failed")
    return shards


def _nodes_for_file(nodes: set[str] | list[str], path: str) -> set[str]:
    prefix = f"{path}::"
    return {node for node in nodes if node.startswith(prefix)}


def get_shard_selectors(
    total_shards: int,
    sqlite_nodes: list[str],
    all_sqlite_nodes: list[str] | set[str] | None = None,
) -> list[list[str]]:
    """Balance file selectors and selected node IDs across independent runners."""
    if total_shards <= 0:
        raise ValueError(f"total_shards must be positive, got {total_shards}")
    selected_nodes = set(sqlite_nodes)
    complete_collection = set(all_sqlite_nodes) if all_sqlite_nodes is not None else selected_nodes
    sqlite_files = sorted(_files_from_nodes(selected_nodes))
    file_set = set(sqlite_files)
    grouped_paths: set[str] = set()
    units: list[tuple[tuple[str, ...], int]] = []
    for group in COLOCATED_TEST_GROUPS:
        selected = tuple(path for path in group if path in file_set)
        if not selected:
            continue
        group_nodes = set().union(*(_nodes_for_file(selected_nodes, path) for path in selected))
        group_is_complete = all(
            _nodes_for_file(complete_collection, path) <= selected_nodes for path in selected
        )
        if group_is_complete:
            units.append((selected, sum(os.path.getsize(path) for path in selected)))
        else:
            per_node_weight = max(1, sum(os.path.getsize(path) for path in selected) // max(1, len(group_nodes)))
            units.extend(((node,), per_node_weight) for node in sorted(group_nodes))
        grouped_paths.update(selected)

    for path in sqlite_files:
        if path in grouped_paths:
            continue
        split_count = SPLIT_TEST_FILES.get(path)
        nodes = [node for node in sqlite_nodes if node.split("::", 1)[0] == path]
        if not split_count:
            file_is_complete = _nodes_for_file(complete_collection, path) <= selected_nodes
            if file_is_complete:
                units.append(((path,), os.path.getsize(path)))
            else:
                per_node_weight = max(1, os.path.getsize(path) // max(1, len(nodes)))
                units.extend(((node,), per_node_weight) for node in sorted(nodes))
            continue
        chunk_size = (len(nodes) + split_count - 1) // split_count
        for offset in range(0, len(nodes), chunk_size):
            chunk = tuple(nodes[offset : offset + chunk_size])
            units.append((chunk, max(1, os.path.getsize(path) * len(chunk) // len(nodes))))

    shards: list[list[str]] = [[] for _ in range(total_shards)]
    shard_weights = [0] * total_shards
    for selectors, weight in sorted(units, key=lambda item: (-item[1], item[0])):
        index = min(range(total_shards), key=lambda item: (shard_weights[item], item))
        shards[index].extend(selectors)
        shard_weights[index] += weight
    return shards


def _has_postgres_marker(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "pytestmark":
                    value = ast.dump(node.value)
                    if POSTGRES_MARKER in value:
                        return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                if POSTGRES_MARKER in ast.dump(decorator):
                    return True
    return False


def audit_postgres_tests(tests_dir: str = "tests") -> tuple[bool, str]:
    unmarked: list[str] = []
    marked_files: list[str] = []
    for path in discover_test_files(tests_dir):
        source = Path(path).read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            return False, f"Cannot parse {path}: {exc}"
        if not any(token in source for token in ("TEST_POSTGRES_DATABASE_URL", "postgresql+asyncpg://")):
            continue
        if _has_postgres_marker(tree):
            marked_files.append(path)
        else:
            unmarked.append(path)

    if unmarked:
        return False, f"PostgreSQL-dependent files without @{POSTGRES_MARKER}: {sorted(unmarked)}"
    if not marked_files:
        return False, "No PostgreSQL integration tests were registered"
    return True, f"Registered PostgreSQL files: {len(marked_files)}"


def verify_collection_coverage(
    total_shards: int = 3,
    tests_dir: str = "tests",
) -> tuple[bool, str]:
    all_nodes = collect_node_ids(tests_dir)
    pg_nodes = collect_node_ids(tests_dir, f"{POSTGRES_EXPRESSION}")
    sqlite_nodes = collect_node_ids(tests_dir, f"not {POSTGRES_EXPRESSION}")
    preflight_nodes = collect_node_ids(
        tests_dir,
        f"not {POSTGRES_EXPRESSION}",
        files=list(PREFLIGHT_TEST_FILES),
    )
    regression_nodes = sqlite_nodes - preflight_nodes
    partitions = {
        "PostgreSQL": pg_nodes,
        "preflight": preflight_nodes,
        "SQLite regression": regression_nodes,
    }
    assigned = set().union(*partitions.values())
    overlaps = [
        (left, right, sorted(partitions[left] & partitions[right]))
        for left in partitions
        for right in partitions
        if left < right and partitions[left] & partitions[right]
    ]
    if overlaps:
        left, right, overlap = overlaps[0]
        return False, f"Job partition overlap ({left}/{right}, {len(overlap)} tests): {overlap[:5]}"
    if all_nodes != assigned:
        missing = sorted(all_nodes - assigned)
        extra = sorted(assigned - all_nodes)
        return False, f"Collection job partition mismatch; missing={missing[:5]}, extra={extra[:5]}"

    sqlite_nodes_ordered = [
        node
        for node in collect_node_id_list(tests_dir, f"not {POSTGRES_EXPRESSION}")
        if node not in preflight_nodes
    ]
    shards = get_shard_selectors(total_shards, sqlite_nodes_ordered, all_sqlite_nodes=sqlite_nodes)
    assigned_nodes: set[str] = set()
    for index, shard in enumerate(shards, start=1):
        shard_nodes = collect_node_ids(tests_dir, f"not {POSTGRES_EXPRESSION}", shard)
        duplicate = assigned_nodes.intersection(shard_nodes)
        if duplicate:
            return False, f"Shard {index} overlaps previous shard: {sorted(duplicate)[:5]}"
        assigned_nodes.update(shard_nodes)
    if assigned_nodes != regression_nodes:
        return False, "Not every SQLite test node is assigned to exactly one shard"

    audit_ok, audit_message = audit_postgres_tests(tests_dir)
    if not audit_ok:
        return False, audit_message
    shard_summary = ", ".join(f"{len(collect_node_ids(tests_dir, f'not {POSTGRES_EXPRESSION}', shard))} tests" for shard in shards)
    return True, (
        f"Collection is exact: {len(all_nodes)} tests = {len(regression_nodes)} SQLite regression + "
        f"{len(preflight_nodes)} preflight + {len(pg_nodes)} PostgreSQL; "
        f"{total_shards} shards ({shard_summary}); {audit_message}."
    )


def _summary_counts(output: str) -> dict[str, int]:
    counts = {key: 0 for key in ("passed", "failed", "skipped", "xfailed", "xpassed", "deselected")}
    summary_lines = [line for line in output.splitlines() if " in " in line and "=" in line]
    summary = summary_lines[-1] if summary_lines else output
    for key in counts:
        match = re.search(rf"(\d+)\s+{key}", summary)
        if match:
            counts[key] = int(match.group(1))
    return counts


def _run_and_require_all(cmd: list[str], expected: int, label: str) -> int:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    output = proc.stdout + proc.stderr
    print(output, end="")
    if proc.returncode != 0:
        return proc.returncode
    counts = _summary_counts(output)
    if counts["passed"] != expected or any(counts[key] for key in ("skipped", "xfailed", "xpassed", "deselected")):
        print(
            f"{label} did not execute the exact collection: expected={expected}, counts={counts}",
            file=sys.stderr,
        )
        return 1
    print(f"{label}: verified {counts['passed']} passed tests with no skipped or deselected tests")
    return 0


def run_preflight(files: list[str] | None = None) -> int:
    selected_files = files or list(PREFLIGHT_TEST_FILES)
    unknown_files = sorted(set(selected_files) - set(PREFLIGHT_TEST_FILES))
    if unknown_files:
        print(f"Unsupported preflight files: {unknown_files}", file=sys.stderr)
        return 1
    expected = len(collect_node_ids(marker=f"not {POSTGRES_EXPRESSION}", files=selected_files))
    if expected == 0:
        print("No preflight tests collected", file=sys.stderr)
        return 1
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-rs",
        "-m",
        f"not {POSTGRES_EXPRESSION}",
        *selected_files,
    ]
    return _run_and_require_all(cmd, expected, "Preflight suite")


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic CI test sharding")
    parser.add_argument("--total-shards", type=int, default=3)
    parser.add_argument("--shard", type=int)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--audit-postgres", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--run-preflight", action="store_true")
    parser.add_argument("--preflight-file", action="append", dest="preflight_files")
    parser.add_argument("--run-postgres", action="store_true")
    args = parser.parse_args()

    if args.verify or args.audit_postgres:
        if args.audit_postgres:
            ok, message = audit_postgres_tests()
            print(message)
            if not ok:
                return 1
        if args.verify:
            ok, message = verify_collection_coverage(args.total_shards)
            print(message)
            return 0 if ok else 1
        return 0

    if args.run_preflight:
        return run_preflight(args.preflight_files)

    if args.run_postgres:
        expected = len(collect_node_ids(marker=POSTGRES_EXPRESSION))
        if expected == 0:
            print("No PostgreSQL tests collected", file=sys.stderr)
            return 1
        pg_files = sorted(_files_from_nodes(collect_node_ids(marker=POSTGRES_EXPRESSION)))
        cmd = [sys.executable, "-m", "pytest", "-q", "-rs", "-m", POSTGRES_EXPRESSION, *pg_files]
        return _run_and_require_all(cmd, expected, "PostgreSQL suite")

    if not args.run or args.shard is None or not 1 <= args.shard <= args.total_shards:
        parser.error("use --run with a valid 1-indexed --shard, or --verify/--audit-postgres")

    all_sqlite_nodes = collect_node_id_list(marker=f"not {POSTGRES_EXPRESSION}")
    preflight_nodes = collect_node_ids(
        marker=f"not {POSTGRES_EXPRESSION}",
        files=list(PREFLIGHT_TEST_FILES),
    )
    sqlite_nodes = [node for node in all_sqlite_nodes if node not in preflight_nodes]
    shards = get_shard_selectors(args.total_shards, sqlite_nodes, all_sqlite_nodes=all_sqlite_nodes)
    selected = shards[args.shard - 1]
    expected = len(collect_node_ids(marker=f"not {POSTGRES_EXPRESSION}", files=selected))
    if expected == 0:
        print(f"Shard {args.shard}/{args.total_shards} has no SQLite tests", file=sys.stderr)
        return 1
    print(
        f"Running SQLite shard {args.shard}/{args.total_shards}: "
        f"{len(_files_from_nodes(set(selected)))} files, {expected} tests"
    )
    cmd = [sys.executable, "-m", "pytest", "-q", "-rs", "-m", f"not {POSTGRES_EXPRESSION}", *selected]
    return _run_and_require_all(cmd, expected, f"SQLite shard {args.shard}/{args.total_shards}")


if __name__ == "__main__":
    raise SystemExit(main())
