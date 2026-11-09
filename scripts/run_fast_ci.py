#!/usr/bin/env python3
"""Run a short, network-isolated validation set for draft pull requests."""

from __future__ import annotations

import importlib
import os
import py_compile
import subprocess
import sys
from pathlib import Path


CORE_IMPORT_MODULES = [
    "config",
    "database",
    "funnel",
    "handlers",
    "main",
    "telegram_client",
]
MANDATORY_TESTS = {"tests/test_startup_router_smoke.py", "tests/test_safety_test_mode.py"}


def check_syntax(root_dir: Path) -> None:
    errors: list[str] = []
    compiled = 0
    for path in root_dir.rglob("*.py"):
        rel = path.relative_to(root_dir)
        if any(part.startswith(".") or part in {"venv", ".venv", "__pycache__"} for part in rel.parts):
            continue
        try:
            py_compile.compile(str(path), doraise=True)
            compiled += 1
        except py_compile.PyCompileError as exc:
            errors.append(f"{rel}: {exc}")
    if errors:
        raise RuntimeError("Syntax errors:\n" + "\n".join(errors))
    print(f"Syntax check passed: {compiled} Python files")


def check_imports() -> None:
    root_dir = str(Path.cwd())
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)
    os.environ.setdefault("TESTING", "1")
    os.environ.setdefault("BOT_TOKEN", "ci-test-token")
    os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/ci_fast.db")
    for module in CORE_IMPORT_MODULES:
        importlib.import_module(module)
    print(f"Core import check passed: {len(CORE_IMPORT_MODULES)} modules")


def changed_files() -> list[str]:
    candidates: list[str] = []
    base = os.environ.get("GITHUB_BASE_REF")
    if base:
        candidates.extend([f"origin/{base}", base])
    candidates.extend(["origin/main", "main", "HEAD~1"])
    chosen = next(
        (ref for ref in candidates if subprocess.run(["git", "rev-parse", "--verify", ref], capture_output=True).returncode == 0),
        None,
    )
    if not chosen:
        return []
    proc = subprocess.run(["git", "diff", "--name-only", f"{chosen}...HEAD"], capture_output=True, text=True)
    return sorted({line.strip() for line in proc.stdout.splitlines() if line.strip()}) if proc.returncode == 0 else []


def targeted_tests(files: list[str]) -> list[str]:
    selected = set(MANDATORY_TESTS)
    for changed in files:
        path = Path(changed)
        if path.parts[:1] == ("tests",) and path.name.startswith("test_") and path.suffix == ".py":
            if path.as_posix() != "tests/test_postgresql_suite.py":
                selected.add(path.as_posix())
            continue
        candidate = Path("tests") / f"test_{path.stem}.py"
        if candidate.exists() and candidate.as_posix() != "tests/test_postgresql_suite.py":
            selected.add(candidate.as_posix())
    return sorted(selected)


def run_tests(test_files: list[str]) -> int:
    cmd = [sys.executable, "-m", "pytest", "-q", "-rs", "-m", "not postgres_integration", *test_files]
    print("Fast CI tests:", " ".join(test_files))
    return subprocess.run(cmd).returncode


def main() -> int:
    root = Path.cwd()
    try:
        check_syntax(root)
        check_imports()
    except Exception as exc:
        print(f"Fast CI preflight failed: {exc}", file=sys.stderr)
        return 1
    files = changed_files()
    print(f"Changed files considered: {len(files)}")
    return run_tests(targeted_tests(files))


if __name__ == "__main__":
    raise SystemExit(main())
