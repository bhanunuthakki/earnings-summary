"""Measure forward returns on our own detected disclosure events (P5).

Layer-3 entrypoint for the P5 build (docs/design/disclosure_change_build_stack.md
§P5). Reads every ``disclosure_events`` row, resolves each one's
information-arrival date (filing date for item-level P0 events, next-filing
date for P1 metric-lifecycle events — see ``filings.event_study``'s module
docstring for exactly why and what gets excluded rather than approximated),
collapses rows that share one underlying filing/return into a single
event-study observation, computes market-adjusted cumulative returns over
four pre-registered windows, controls for the earnings surprise via one
pooled regression per window, and reports a bootstrap 95% CI per
(bucket, window) cell.

Read-only against ``disclosure_events`` / ``filing_sections`` /
``earnings_surprises`` — writes nothing to the DB. The full report (every
bucket, every skipped event with its reason) is written to
``.tmp/event_study/<run-id>.json``; stdout carries only the top-line ALL and
per-event-type cells plus the file path, per the repo's >100KB-to-.tmp rule.

Structured events go to stderr, one JSON object per line. Exit codes: 0
success, 1 hard stop (missing table/migration), 2 bad arguments.

Usage:
    python execution/measure_disclosure_event_returns.py
    python execution/measure_disclosure_event_returns.py --tickers MELI,NU,WIX
    python execution/measure_disclosure_event_returns.py --benchmark SPY --out .tmp/event_study/run1.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from filings.event_study import (  # noqa: E402
    DEFAULT_BENCHMARK_TICKER,
    EventStudyReport,
    run_event_study,
)

_EXIT_HARD_STOP = 1
_EXIT_BAD_ARGS = 2


class _JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = (
            cast("dict[str, object]", record.msg)
            if isinstance(record.msg, dict)
            else {"message": record.getMessage()}
        )
        return json.dumps({"level": record.levelname, **payload}, default=str)


def _configure_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonLineFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def _require_tables(conn: sqlite3.Connection) -> str | None:
    existing = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing = [
        t
        for t in ("disclosure_events", "filing_sections", "earnings_surprises")
        if t not in existing
    ]
    if missing:
        return f"missing tables: {', '.join(missing)}"
    return None


def _default_out_path(repo_root: Path) -> Path:
    out_dir = repo_root / ".tmp" / "event_study"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).replace(tzinfo=None).strftime("%Y%m%dT%H%M%S")
    return out_dir / f"event_study_{stamp}.json"


def _print_headline(report: EventStudyReport, log: logging.Logger) -> None:
    for b in report.buckets:
        if b.bucket == "ALL" or (b.bucket.startswith("event_type=") and "|" not in b.bucket):
            log.info(
                {
                    "event": "bucket_result",
                    "bucket": b.bucket,
                    "window": b.window,
                    "n": b.n,
                    "n_unique_ticker_dates": b.n_unique_ticker_dates,
                    "mean_adjusted_car_pct": b.mean_adjusted_car_pct,
                    "ci95_adjusted_pct": [
                        b.ci95_adjusted_low_pct,
                        b.ci95_adjusted_high_pct,
                    ],
                    "mean_surprise_controlled_car_pct": b.mean_surprise_controlled_car_pct,
                    "underpowered": b.underpowered,
                }
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--tickers", type=str, default=None, help="Comma-separated tickers (default: all)"
    )
    parser.add_argument("--db-path", type=str, default=None, help="Portfolio DB path override")
    parser.add_argument(
        "--repo-root",
        type=str,
        default=None,
        help=(
            "Repo root to read cached price history from (data/historical/fmp/...). "
            "Defaults to this script's own repo root, which is WRONG when running "
            "from a worktree against a --db-path in a different checkout (data/ is "
            "gitignored and worktrees don't have it) — pass this explicitly in that "
            "case. Defaults to --db-path's grandparent directory when --db-path is "
            "given and --repo-root is not."
        ),
    )
    parser.add_argument(
        "--benchmark",
        type=str,
        default=DEFAULT_BENCHMARK_TICKER,
        help=f"Benchmark ticker for return adjustment (default: {DEFAULT_BENCHMARK_TICKER})",
    )
    parser.add_argument(
        "--out", type=str, default=None, help="Output JSON path (default: .tmp/event_study/...)"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    log = logging.getLogger("measure_disclosure_event_returns")

    if args.db_path:
        db.set_db_path(args.db_path)
    conn = db.get_connection()

    if args.repo_root:
        repo_root = Path(args.repo_root)
    elif args.db_path:
        # db_path is conventionally <repo_root>/data/portfolio.db.
        repo_root = Path(args.db_path).resolve().parent.parent
    else:
        repo_root = PROJECT_ROOT
    log.info({"event": "repo_root_resolved", "repo_root": str(repo_root)})

    try:
        hard_stop = _require_tables(conn)
        if hard_stop:
            log.error({"event": "hard_stop", "stage": "preflight", "error": hard_stop})
            return _EXIT_HARD_STOP

        tickers = (
            [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
            if args.tickers
            else None
        )
        if args.tickers and not tickers:
            log.error({"event": "empty_ticker_set"})
            return _EXIT_BAD_ARGS

        started = datetime.now(UTC).replace(tzinfo=None)
        report = run_event_study(conn, repo_root, tickers=tickers, benchmark_ticker=args.benchmark)
        finished = datetime.now(UTC).replace(tzinfo=None)

        out_path = Path(args.out) if args.out else _default_out_path(repo_root)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(),
                    "tickers": tickers,
                    "report": report.model_dump(mode="json"),
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

        log.info(
            {
                "event": "run_complete",
                "n_events_total": report.n_events_total,
                "n_observations_collapsed": report.n_observations_collapsed,
                "n_events_skipped": report.n_events_skipped,
                "skip_reasons": report.skip_reasons,
                "out_path": str(out_path),
            }
        )
        _print_headline(report, log)

        summary: dict[str, object] = {
            "out_path": str(out_path),
            "n_events_total": report.n_events_total,
            "n_observations_collapsed": report.n_observations_collapsed,
            "n_events_skipped": report.n_events_skipped,
            "skip_reasons": report.skip_reasons,
            "regressions": [r.model_dump(mode="json") for r in report.regressions],
            "headline_buckets": [
                b.model_dump(mode="json")
                for b in report.buckets
                if b.bucket == "ALL" or ("|" not in b.bucket and b.bucket.startswith("event_type="))
            ],
        }
        json.dump(summary, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
