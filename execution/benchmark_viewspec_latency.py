"""Measure ViewSpec per-interaction read latency and emit one JSON receipt.

A rerun-per-interaction analytics surface (Streamlit and friends) re-executes
its script on every widget change, so ``metric_catalog`` and ``execute_view``
latency decides whether such a surface is viable. This entrypoint measures both
and fails closed on the budgets.

Synthetic clone (creates a NEW disposable database, never an authority)::

    python execution/sqlite_bootstrap.py execution/benchmark_viewspec_latency.py \
        --mode synthetic --database .tmp/viewspec_latency.db \
        --output .tmp/viewspec_latency_report.json \
        --tickers 40 --line-items 150 --kpis 25 --quarters 40 --sweep

Restored provenance-bearing snapshot (read-only, no seeding)::

    python execution/sqlite_bootstrap.py execution/benchmark_viewspec_latency.py \
        --mode snapshot --database .tmp/<restored-snapshot>.db \
        --output .tmp/viewspec_latency_report.json --ticker NU --ticker MELI

Exit codes: 0 pass, 1 refused, 2 budget failure.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

# ``_lib`` is the sanctioned execution helper that puts ``src`` on the import
# path; importing it first keeps this entrypoint free of its own sys.path edit.
from _lib import command_parser, log_event
from pydantic import BaseModel, ConfigDict

from viewspec.latency_benchmark import (
    LatencyBudgets,
    RefusedBenchmarkPathError,
    SyntheticScale,
    run_benchmark,
    seed_synthetic_database,
    synthetic_tickers,
    write_report_atomic,
)


class _Arguments(BaseModel):
    """One validated boundary between argparse and typed benchmark code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["synthetic", "snapshot"]
    database: Path
    output: Path
    samples: int
    sweep: bool
    periods: int
    ticker: list[str]
    tickers: int
    line_items: int
    kpis: int
    quarters: int
    documents_per_ticker: int
    max_catalog_cold_ms: float
    max_view_small_p95_ms: float
    max_view_full_p95_ms: float
    max_cached_payload_p95_ms: float


def _parser() -> argparse.ArgumentParser:
    parser = command_parser(
        "Measure ViewSpec catalog and view latency for a rerun-per-interaction surface"
    )
    parser.add_argument("--mode", choices=("synthetic", "snapshot"), required=True)
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="synthetic: NEW disposable clone path; snapshot: existing database to read",
    )
    parser.add_argument("--output", type=Path, required=True, help="JSON receipt path")
    parser.add_argument("--samples", type=int, default=5, help="Timed repetitions per call")
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Also measure latency by selected-ticker count and requested cell shape",
    )
    parser.add_argument("--periods", type=int, default=12, help="Buckets per measured view")
    parser.add_argument(
        "--ticker",
        action="append",
        default=[],
        help="snapshot mode: a symbol to measure (repeatable)",
    )
    parser.add_argument("--tickers", type=int, default=40, help="synthetic: issuer count")
    parser.add_argument("--line-items", type=int, default=150, help="synthetic: items per issuer")
    parser.add_argument("--kpis", type=int, default=25, help="synthetic: KPI names per issuer")
    parser.add_argument("--quarters", type=int, default=40, help="synthetic: quarters per series")
    parser.add_argument("--documents-per-ticker", type=int, default=40)
    parser.add_argument("--max-catalog-cold-ms", type=float, default=2_000.0)
    parser.add_argument("--max-view-small-p95-ms", type=float, default=700.0)
    parser.add_argument("--max-view-full-p95-ms", type=float, default=1_000.0)
    parser.add_argument("--max-cached-payload-p95-ms", type=float, default=50.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _Arguments.model_validate(vars(_parser().parse_args(argv)))

    if args.database.resolve() == args.output.resolve():
        log_event("viewspec_latency_refused", reason="database and output paths must differ")
        return 1

    budgets = LatencyBudgets(
        max_catalog_cold_milliseconds=args.max_catalog_cold_ms,
        max_view_small_p95_milliseconds=args.max_view_small_p95_ms,
        max_view_full_p95_milliseconds=args.max_view_full_p95_ms,
        max_cached_payload_p95_milliseconds=args.max_cached_payload_p95_ms,
    )

    scale: SyntheticScale | None = None
    try:
        if args.mode == "synthetic":
            scale = SyntheticScale(
                tickers=args.tickers,
                line_items=args.line_items,
                kpis=args.kpis,
                quarters=args.quarters,
                documents_per_ticker=args.documents_per_ticker,
            )
            log_event(
                "viewspec_latency_seed_started",
                database=str(args.database),
                financial_fact_rows=scale.financial_fact_rows,
                kpi_fact_rows=scale.kpi_fact_rows,
            )
            seed_synthetic_database(args.database, scale)
            tickers = synthetic_tickers(scale)
        else:
            tickers = [symbol.strip().upper() for symbol in args.ticker if symbol.strip()]
            if not tickers:
                log_event(
                    "viewspec_latency_refused",
                    reason="snapshot mode requires at least one --ticker",
                )
                return 1
            if not args.database.exists():
                log_event("viewspec_latency_refused", reason=f"database not found: {args.database}")
                return 1

        log_event("viewspec_latency_measure_started", tickers=len(tickers), samples=args.samples)
        report = run_benchmark(
            db_path=args.database,
            tickers=tickers,
            budgets=budgets,
            samples=args.samples,
            mode=args.mode,
            scale=scale,
            periods=args.periods,
            sweep=args.sweep,
        )
        write_report_atomic(report, args.output)
    except RefusedBenchmarkPathError as exc:
        log_event("viewspec_latency_refused", reason=str(exc))
        return 1

    log_event(
        "viewspec_latency_finished",
        output=str(args.output.resolve()),
        overall_pass=report.overall_pass,
        report_sha256=report.report_sha256,
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "overall_pass": report.overall_pass,
                "catalog_cold_ms": round(report.catalog.latency.max_milliseconds, 1),
                "view_small_p95_ms": round(report.small_view.latency.p95_milliseconds, 1),
                "view_full_p95_ms": round(report.full_view.latency.p95_milliseconds, 1),
                "cached_payload_p95_ms": round(
                    report.full_view.cached_payload_latency.p95_milliseconds, 1
                ),
                "empty_relations": list(report.empty_relations),
            },
            sort_keys=True,
        )
    )
    return 0 if report.overall_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
