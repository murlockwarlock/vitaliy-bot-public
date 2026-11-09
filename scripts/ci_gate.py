#!/usr/bin/env python3
"""Fail-closed gate for draft fast CI and mandatory full CI."""

from __future__ import annotations

import sys


def evaluate_ci_gate(
    mode: str,
    fast_status: str,
    preflight_status: str,
    sqlite_status: str,
    postgres_status: str,
) -> tuple[bool, str]:
    values = {
        "fast-ci": (fast_status or "").strip().lower(),
        "preflight": (preflight_status or "").strip().lower(),
        "sqlite": (sqlite_status or "").strip().lower(),
        "postgres": (postgres_status or "").strip().lower(),
    }
    mode = (mode or "").strip().lower()
    if mode == "draft":
        expected = {"fast-ci": "success", "preflight": "skipped", "sqlite": "skipped", "postgres": "skipped"}
    elif mode == "full":
        expected = {"fast-ci": "skipped", "preflight": "success", "sqlite": "success", "postgres": "success"}
    else:
        return False, f"CI Gate FAILED: unknown mode '{mode}'"

    failures = [f"{name}={values[name]} (expected {expected[name]})" for name in expected if values[name] != expected[name]]
    if failures:
        return False, "CI Gate FAILED: " + "; ".join(failures)
    return True, f"CI Gate passed in {mode} mode: all required checks have expected states"


def main() -> int:
    if len(sys.argv) != 6:
        print(f"Usage: {sys.argv[0]} <draft|full> <fast> <preflight> <sqlite> <postgres>")
        return 1
    ok, message = evaluate_ci_gate(*sys.argv[1:])
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
