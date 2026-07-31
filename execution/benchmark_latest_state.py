"""Run the provider-free latest-governed-state benchmark."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_PROFILE_DATABASE = (
    PROJECT_ROOT / ".tmp" / "latest_state_benchmark" / "production-profile.db"
)
PRODUCTION_PROFILE_REPORT = (
    PROJECT_ROOT / "output" / "latest_state_benchmark" / "production-profile.json"
)
PRODUCTION_PROFILE_COMMAND = (
    r".\venv\Scripts\python.exe execution\benchmark_latest_state.py "
    "--profile production --confirm-production-profile"
)
HARD_RUNTIME_CEILING_SECONDS = 1_500.0


class HardRuntimeCeiling:
    """Cancel-aware watchdog for the command runtime, not a benchmark budget."""

    def __init__(self, *, seconds: float, on_timeout: Callable[[], None]) -> None:
        if seconds <= 0:
            raise ValueError("hard runtime ceiling must be positive")
        self._seconds = seconds
        self._on_timeout = on_timeout
        self._cancelled = threading.Event()
        self._thread = threading.Thread(
            target=self._wait,
            name="latest-state-benchmark-runtime-ceiling",
            daemon=True,
        )

    def _wait(self) -> None:
        if not self._cancelled.wait(self._seconds):
            self._on_timeout()

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)


def _event(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), file=sys.stderr)


def _hard_timeout() -> None:
    _event(
        "latest_state_benchmark_hard_timeout",
        hard_runtime_ceiling_seconds=HARD_RUNTIME_CEILING_SECONDS,
        result="not_a_benchmark_result",
    )
    sys.stderr.flush()
    os._exit(124)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark no-op, small-delta, current reads, storage, resume, "
            "and retained-history independence in a new isolated SQLite DB"
        ),
        epilog=(
            f"Production command: {PRODUCTION_PROFILE_COMMAND}. "
            f"Default report: {PRODUCTION_PROFILE_REPORT}"
        ),
    )
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--profile",
        choices=("smoke", "production"),
        default="smoke",
        help=(
            "production uses 1,284 publications, 831,471 cells, 87 scopes, "
            "and the current blocker inventory of 24 documents at 8 chunks each"
        ),
    )
    parser.add_argument("--publication-count", type=int)
    parser.add_argument("--cell-count", type=int)
    parser.add_argument("--document-count", type=int)
    parser.add_argument("--chunk-count", type=int)
    parser.add_argument("--scope-count", type=int)
    parser.add_argument("--delta-publication-count", type=int)
    parser.add_argument("--delta-cell-count", type=int)
    parser.add_argument("--delta-document-count", type=int)
    parser.add_argument("--delta-chunk-count", type=int)
    parser.add_argument("--max-batch-rows", type=int)
    parser.add_argument("--read-samples", type=int)
    parser.add_argument("--read-limit", type=int)
    parser.add_argument("--interrupt-after-batches", type=int)
    parser.add_argument(
        "--confirm-production-profile",
        action="store_true",
        help=(
            "Required for the large production profile; defaults its isolated "
            "database and report to the documented project paths"
        ),
    )
    parser.add_argument(
        "--max-hot-path-seconds",
        type=float,
        help="Budget only the post-fixture no-op/delta/read/resume/history workload",
    )
    parser.add_argument("--max-peak-python-memory-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--max-allocated-sqlite-pages", type=int, default=250_000)
    parser.add_argument("--max-noop-milliseconds", type=float, default=1_000.0)
    parser.add_argument("--max-small-delta-milliseconds", type=float, default=5_000.0)
    parser.add_argument("--max-fact-read-p95-milliseconds", type=float, default=100.0)
    parser.add_argument("--max-narrative-read-p95-milliseconds", type=float, default=100.0)
    parser.add_argument("--max-history-latency-ratio", type=float, default=1.50)
    return parser


def main(argv: list[str] | None = None) -> int:
    project_src = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(project_src))
    from provenance.latest_state_benchmark import (
        LatestStateBenchmarkBudgets,
        LatestStateBenchmarkConfig,
        LatestStateSqliteAdapter,
        RefusedBenchmarkPathError,
        preflight_benchmark_paths,
        production_benchmark_budgets,
        production_benchmark_config,
        run_latest_state_benchmark,
        write_report_atomic,
    )

    args = _parser().parse_args(argv)
    dimension_names = (
        "publication_count",
        "cell_count",
        "document_count",
        "chunk_count",
        "scope_count",
        "delta_publication_count",
        "delta_cell_count",
        "delta_document_count",
        "delta_chunk_count",
        "max_batch_rows",
        "read_samples",
        "read_limit",
        "interrupt_after_batches",
    )
    if args.profile == "production":
        if not args.confirm_production_profile:
            _event(
                "latest_state_benchmark_refused",
                reason="production profile requires --confirm-production-profile",
                command=PRODUCTION_PROFILE_COMMAND,
                report=str(PRODUCTION_PROFILE_REPORT),
            )
            return 1
        overridden = [name for name in dimension_names if getattr(args, name) is not None]
        if overridden:
            _event(
                "latest_state_benchmark_refused",
                reason="production profile dimensions cannot be overridden",
                overrides=overridden,
            )
            return 1
        if args.max_hot_path_seconds is not None and args.max_hot_path_seconds > 900.0:
            _event(
                "latest_state_benchmark_refused",
                reason="production profile hot-path budget cannot exceed 900 seconds",
            )
            return 1
        database = args.database or PRODUCTION_PROFILE_DATABASE
        output = args.output or PRODUCTION_PROFILE_REPORT
    else:
        if args.confirm_production_profile:
            _event(
                "latest_state_benchmark_refused",
                reason="--confirm-production-profile requires --profile production",
            )
            return 1
        if args.database is None or args.output is None:
            _event(
                "latest_state_benchmark_refused",
                reason="smoke profile requires --database and --output",
            )
            return 1
        database = args.database
        output = args.output
    base = (
        production_benchmark_config()
        if args.profile == "production"
        else LatestStateBenchmarkConfig()
    )
    overrides = {
        name: getattr(args, name) for name in dimension_names if getattr(args, name) is not None
    }
    config = base.model_copy(update=overrides)
    config = LatestStateBenchmarkConfig.model_validate(config.model_dump())
    base_budgets = (
        production_benchmark_budgets()
        if args.profile == "production"
        else LatestStateBenchmarkBudgets()
    )
    budgets = LatestStateBenchmarkBudgets(
        max_hot_path_seconds=(
            base_budgets.max_hot_path_seconds
            if args.max_hot_path_seconds is None
            else args.max_hot_path_seconds
        ),
        max_peak_python_memory_bytes=args.max_peak_python_memory_bytes,
        max_allocated_sqlite_pages=args.max_allocated_sqlite_pages,
        max_noop_milliseconds=args.max_noop_milliseconds,
        max_small_delta_milliseconds=args.max_small_delta_milliseconds,
        max_fact_read_p95_milliseconds=args.max_fact_read_p95_milliseconds,
        max_narrative_read_p95_milliseconds=args.max_narrative_read_p95_milliseconds,
        max_history_latency_ratio=args.max_history_latency_ratio,
    )
    try:
        database, output = preflight_benchmark_paths(database, output)
    except RefusedBenchmarkPathError as exc:
        _event("latest_state_benchmark_refused", reason=str(exc))
        return 1
    _event(
        "latest_state_benchmark_started",
        profile=config.profile,
        database=str(database),
        output=str(output),
        requested=config.model_dump(mode="json"),
        hard_runtime_ceiling_seconds=HARD_RUNTIME_CEILING_SECONDS,
    )
    runtime_ceiling = HardRuntimeCeiling(
        seconds=HARD_RUNTIME_CEILING_SECONDS,
        on_timeout=_hard_timeout,
    )
    runtime_ceiling.start()
    try:
        report = run_latest_state_benchmark(
            config=config,
            budgets=budgets,
            database_path=database,
            adapter=LatestStateSqliteAdapter(),
        )
        write_report_atomic(report, output)
    finally:
        runtime_ceiling.cancel()
    _event(
        "latest_state_benchmark_finished",
        output=str(output),
        effective=report.fixture.model_dump(mode="json"),
        overall_pass=report.overall_pass,
        report_sha256=report.report_sha256,
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "overall_pass": report.overall_pass,
                "report_sha256": report.report_sha256,
            },
            sort_keys=True,
        )
    )
    return 0 if report.overall_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
