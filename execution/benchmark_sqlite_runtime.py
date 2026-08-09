"""Bounded before/after benchmark for the managed SQLite preload seam."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import TypedDict, cast


class ProbeResult(TypedDict):
    sqlite_version: str
    iterations: int
    connection_read_us: float
    request_scoped_read_us: float


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = PROJECT_ROOT / "execution" / "sqlite_bootstrap.py"
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _probe(iterations: int) -> ProbeResult:
    import sqlite3

    started = time.perf_counter()
    for _ in range(iterations):
        connection = sqlite3.connect(":memory:")
        try:
            assert connection.execute("SELECT 1").fetchone()[0] == 1
        finally:
            connection.close()
    elapsed = time.perf_counter() - started
    connection = sqlite3.connect(":memory:")
    try:
        started_scoped = time.perf_counter()
        for _ in range(iterations):
            assert connection.execute("SELECT 1").fetchone()[0] == 1
        scoped_elapsed = time.perf_counter() - started_scoped
    finally:
        connection.close()
    return {
        "sqlite_version": sqlite3.sqlite_version,
        "iterations": iterations,
        "connection_read_us": elapsed * 1_000_000 / iterations,
        "request_scoped_read_us": scoped_elapsed * 1_000_000 / iterations,
    }


def _run(command: list[str]) -> tuple[ProbeResult, float]:
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    process_ms = (time.perf_counter() - started) * 1_000
    result = cast("ProbeResult", json.loads(completed.stdout))
    return result, process_ms


def _driver(iterations: int, repetitions: int) -> dict[str, object]:
    raw_command = [sys.executable, __file__, "--probe", "--iterations", str(iterations)]
    managed_command = [
        sys.executable,
        str(BOOTSTRAP),
        __file__,
        "--probe",
        "--iterations",
        str(iterations),
    ]
    raw_runs: list[tuple[ProbeResult, float]] = []
    managed_runs: list[tuple[ProbeResult, float]] = []
    for repetition in range(repetitions):
        commands = (
            ((raw_command, raw_runs), (managed_command, managed_runs))
            if repetition % 2 == 0
            else ((managed_command, managed_runs), (raw_command, raw_runs))
        )
        for command, destination in commands:
            destination.append(_run(command))
    raw_connection_us = statistics.median(run[0]["connection_read_us"] for run in raw_runs)
    managed_connection_us = statistics.median(run[0]["connection_read_us"] for run in managed_runs)
    raw_scoped_read_us = statistics.median(run[0]["request_scoped_read_us"] for run in raw_runs)
    managed_scoped_read_us = statistics.median(
        run[0]["request_scoped_read_us"] for run in managed_runs
    )
    raw_process_ms = statistics.median(run[1] for run in raw_runs)
    managed_process_ms = statistics.median(run[1] for run in managed_runs)
    return {
        "iterations_per_process": iterations,
        "repetitions": repetitions,
        "raw": {
            "sqlite_version": raw_runs[0][0]["sqlite_version"],
            "connection_read_us": round(raw_connection_us, 3),
            "request_scoped_read_us": round(raw_scoped_read_us, 3),
            "median_process_ms": round(raw_process_ms, 3),
        },
        "managed": {
            "sqlite_version": managed_runs[0][0]["sqlite_version"],
            "connection_read_us": round(managed_connection_us, 3),
            "request_scoped_read_us": round(managed_scoped_read_us, 3),
            "median_process_ms": round(managed_process_ms, 3),
        },
        "delta": {
            "connection_read_percent": round(
                (managed_connection_us / raw_connection_us - 1) * 100,
                2,
            ),
            "request_scoped_read_percent": round(
                (managed_scoped_read_us / raw_scoped_read_us - 1) * 100,
                2,
            ),
            "startup_and_process_ms": round(managed_process_ms - raw_process_ms, 3),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--iterations", type=int, default=250)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument(
        "--production-overview-db",
        type=Path,
        help="benchmark the actual Overview DB-read path against a private copy of this DB",
    )
    args = parser.parse_args(argv)
    if args.iterations <= 0 or args.repetitions <= 0:
        parser.error("iterations and repetitions must be positive")
    if args.probe and args.production_overview_db is not None:
        parser.error("--probe and --production-overview-db are mutually exclusive")
    if args.production_overview_db is not None:
        from sqlite_overview_benchmark import benchmark_production_overview

        payload: object = benchmark_production_overview(
            args.production_overview_db,
            args.iterations,
        )
    else:
        payload = (
            _probe(args.iterations) if args.probe else _driver(args.iterations, args.repetitions)
        )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
