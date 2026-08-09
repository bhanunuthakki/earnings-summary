"""Benchmark synthetic data-infrastructure contracts in an isolated database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _event(event: str, **fields: object) -> None:
    print(
        json.dumps({"event": event, **fields}, sort_keys=True),
        file=sys.stderr,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a deterministic provider-free benchmark against a new synthetic SQLite database"
        )
    )
    parser.add_argument(
        "--mode",
        choices=("synthetic", "production_contract"),
        default="synthetic",
        help=(
            "synthetic uses the minimal contract-equivalent schema; "
            "production_contract migrates a new DB and calls real 0246/0247 APIs"
        ),
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="New caller-selected path for the isolated synthetic SQLite DB",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Caller-selected path for the canonical atomic JSON report",
    )
    parser.add_argument("--fact-count", type=int, required=True)
    parser.add_argument(
        "--delta-count",
        type=int,
        required=True,
        help=(
            "Synthetic delta row count; accepted but not used by "
            "production_contract, whose real delta is a zero-change smoke"
        ),
    )
    parser.add_argument("--chunk-size", type=int, required=True)
    parser.add_argument("--page-size", type=int, required=True)
    parser.add_argument("--read-samples", type=int, required=True)
    parser.add_argument("--max-total-seconds", type=float, required=True)
    parser.add_argument(
        "--max-peak-python-memory-bytes",
        type=int,
        required=True,
    )
    parser.add_argument("--max-database-bytes", type=int, required=True)
    parser.add_argument(
        "--min-stream-rows-per-second",
        type=float,
        required=True,
    )
    parser.add_argument(
        "--min-projection-rows-per-second",
        type=float,
        required=True,
    )
    parser.add_argument(
        "--max-point-p95-milliseconds",
        type=float,
        required=True,
    )
    parser.add_argument(
        "--max-page-p95-milliseconds",
        type=float,
        required=True,
    )
    parser.add_argument(
        "--max-full-audit-seconds",
        type=float,
        required=True,
    )
    parser.add_argument(
        "--max-bucket-audit-seconds",
        type=float,
        required=True,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Direct script execution puts execution/, not the repository root, on
    # sys.path. Delay this project import until after the explicit src bootstrap.
    project_src = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(project_src))
    from provenance.data_infrastructure_benchmark import (
        BenchmarkBudgets,
        BenchmarkConfig,
        ProductionBenchmarkConfig,
        RefusedBenchmarkPathError,
        run_benchmark,
        run_production_contract_benchmark,
        write_report_atomic,
    )

    args = _parser().parse_args(argv)
    config = (
        ProductionBenchmarkConfig(
            fact_count=args.fact_count,
            publication_chunk_size=args.chunk_size,
            page_size=args.page_size,
            read_samples=args.read_samples,
        )
        if args.mode == "production_contract"
        else BenchmarkConfig(
            fact_count=args.fact_count,
            delta_count=args.delta_count,
            chunk_size=args.chunk_size,
            page_size=args.page_size,
            read_samples=args.read_samples,
        )
    )
    budgets = BenchmarkBudgets(
        max_total_seconds=args.max_total_seconds,
        max_peak_python_memory_bytes=args.max_peak_python_memory_bytes,
        max_database_bytes=args.max_database_bytes,
        min_stream_rows_per_second=args.min_stream_rows_per_second,
        min_projection_rows_per_second=args.min_projection_rows_per_second,
        max_point_p95_milliseconds=args.max_point_p95_milliseconds,
        max_page_p95_milliseconds=args.max_page_p95_milliseconds,
        max_full_audit_seconds=args.max_full_audit_seconds,
        max_bucket_audit_seconds=args.max_bucket_audit_seconds,
    )
    _event(
        "data_infrastructure_benchmark_started",
        database=str(args.database),
        output=str(args.output),
        fact_count=config.fact_count,
        benchmark_mode=args.mode,
    )
    if args.database.resolve() == args.output.resolve():
        _event(
            "data_infrastructure_benchmark_refused",
            reason="synthetic database and report paths must differ",
        )
        return 1
    try:
        report = (
            run_production_contract_benchmark(
                config=config,
                budgets=budgets,
                database_path=args.database,
            )
            if isinstance(config, ProductionBenchmarkConfig)
            else run_benchmark(
                config=config,
                budgets=budgets,
                database_path=args.database,
            )
        )
        write_report_atomic(report, args.output)
    except RefusedBenchmarkPathError as exc:
        _event(
            "data_infrastructure_benchmark_refused",
            reason=str(exc),
        )
        return 1
    _event(
        "data_infrastructure_benchmark_finished",
        output=str(args.output.resolve()),
        overall_pass=report.overall_pass,
        config_sha256=report.config_sha256,
        report_sha256=report.report_sha256,
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "overall_pass": report.overall_pass,
                "report_sha256": report.report_sha256,
            },
            sort_keys=True,
        )
    )
    return 0 if report.overall_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
