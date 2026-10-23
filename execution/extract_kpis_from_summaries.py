"""Stages 2-3 of the KPI cascade: extract tier_1 KPIs from `.tmp/` LLM summaries.

Reads each holding's tier_1_kpis and extracts the full list from each distinct
summary through the governed model route. Exact completed material inputs skip
model work. Empty and partial responses complete bounded processing; unreturned
metrics remain unknown. Facts persist through the existing manifest pipeline.

`--source` selects the document family:
  earnings  Stage 2: per-quarter call summary + investor-update variant
            (`{T}_Q{N}_{Y}_summary.txt`, `{T}_Q{N}_{Y}_investor_update_summary.txt`)
  ir        Stage 3: IR-pipeline press-release + presentation briefs
            (`{T}_Q{N}_{Y}_press_release_summary.txt`, `..._presentation_brief.txt`)

`--capture` switches to capture-every-number mode: enumerate EVERY numeric fact
in each brief (no tier_1 allowlist) and persist with origin=CAPTURE.

Usage:
    python execution/extract_kpis_from_summaries.py --ticker NU
    python execution/extract_kpis_from_summaries.py --all
    python execution/extract_kpis_from_summaries.py --all --source ir
    python execution/extract_kpis_from_summaries.py --all --refresh
    python execution/extract_kpis_from_summaries.py --ticker NU --source ir --capture

Per-run telemetry lands in `data/kpi_extraction_log.json` keyed by ticker → stage.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

try:  # direct script invocation
    from _lib import PROJECT_ROOT, add_database_argument, require_db_path
except ImportError:  # pragma: no cover - package import fallback
    from execution._lib import PROJECT_ROOT, add_database_argument, require_db_path

import db
from compute.kpi_extract_summaries import (
    TickerExtractionLog,
    capture_for_ticker,
    extract_for_ticker,
    write_log,
)
from db_paths import db_path_context
from pipeline.run_accounting import (
    PipelineRunSuppressedError,
    suppression_payload,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    try:
        db_path = require_db_path(args.db_path)
    except (OSError, RuntimeError) as exc:
        print(f"[error] refusing to run: {exc}", file=sys.stderr)
        return 1

    results: list[TickerExtractionLog] = []
    summary_lines: list[dict[str, object]] = []
    with (
        db_path_context(db_path),
        closing(
            connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        ) as conn,
    ):
        conn.row_factory = sqlite3.Row
        tickers = _resolve_tickers(conn, args)
        if not tickers:
            print("[]")
            return 0
        try:
            for ticker in tickers:
                if args.capture:
                    log = capture_for_ticker(
                        ticker,
                        repo_root,
                        conn,
                        source_group=args.source,
                        refresh=args.refresh,
                    )
                else:
                    log = extract_for_ticker(
                        ticker,
                        repo_root,
                        conn,
                        refresh=args.refresh,
                        source_group=args.source,
                        requested=args.requested,
                    )
                results.append(log)
                summary_lines.append(
                    {
                        "ticker": log.ticker,
                        "extracted_quarters": len(log.quarters_extracted),
                        "skipped_quarters": len(log.quarters_skipped_no_missing),
                        "kpis_inserted": log.kpis_inserted_total,
                        "elapsed_ms": log.elapsed_ms,
                        "error": log.error,
                    }
                )
                print(
                    f"[{log.ticker}] extracted={len(log.quarters_extracted)} "
                    f"skipped={len(log.quarters_skipped_no_missing)} "
                    f"kpis_inserted={log.kpis_inserted_total} "
                    f"elapsed={log.elapsed_ms}ms" + (f" error={log.error}" if log.error else ""),
                    file=sys.stderr,
                )
        except PipelineRunSuppressedError as exc:
            print(json.dumps(suppression_payload(exc)))
            return 0

    log_path = write_log(repo_root, results)
    print(json.dumps({"summary": summary_lines, "log": str(log_path)}, indent=2))
    return 1 if any(log.error for log in results) else 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker")
    g.add_argument("--all", action="store_true", help="All portfolio + watchlist tickers")
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Repeat completed allowlist extraction; active allowlist attempts remain protected",
    )
    p.add_argument(
        "--capture",
        action="store_true",
        help="Capture-every-number mode: enumerate EVERY numeric fact (no tier_1 "
        "allowlist) and persist with origin=CAPTURE. Default is the allowlist path.",
    )
    p.add_argument(
        "--source",
        choices=("earnings", "ir"),
        default="earnings",
        help="Which document family to extract from (default: earnings)",
    )
    p.add_argument(
        "--requested",
        action="store_true",
        help="Retain owner request context for --ticker evidence preparation",
    )
    add_database_argument(p)
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT, help="Source and artifact root")
    args = p.parse_args()
    if args.requested and not args.ticker:
        p.error("--requested requires one --ticker")
    return args


def _resolve_tickers(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    cur = conn.cursor()
    cur.execute(
        f"SELECT DISTINCT ticker FROM tracked_companies "
        f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} ORDER BY ticker"
    )
    rows = cur.fetchall()
    return [r[0] for r in rows]


if __name__ == "__main__":
    raise SystemExit(main())
