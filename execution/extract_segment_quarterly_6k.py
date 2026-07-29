"""Extract quarterly segment/product-revenue data from FPI 6-K exhibits into
the segment_periods + segment_dimensions junction (docs/design/
segment_quarterly_framework.md, Phase 3 -- ``fpi_6k`` route, spike-gated).

Usage:
    python execution/extract_segment_quarterly_6k.py --ticker NU --year 2026 --quarter Q1
    python execution/extract_segment_quarterly_6k.py --ticker NVO --year 2026 --quarter Q1
    python execution/extract_segment_quarterly_6k.py --all --year 2026 --quarter Q1
    python execution/extract_segment_quarterly_6k.py --all --lookback-quarters 4

Routing: only tickers whose ``tracked_companies.filing_regime == '20-F'``
AND whose Phase-3 spike classification is ``"supported"``
(``compute.segment_quarterly_6k._TICKER_6K_STATUS`` -- currently NU/NVO/WIX;
ASML is spike-confirmed negative and any other 20-F/40-F name is untested)
are ever fetched over the network. Everything else records or leaves in
place an honest ``segment_quarterly_coverage`` reason code -- never a blind
attempt.

``--all`` without an explicit ``--year``/``--quarter`` walks the last
``--lookback-quarters`` (default 4) nominal quarters ending at the most
recently *fully elapsed* calendar quarter, for every fpi_6k-routed ticker --
network calls are cheap to re-run (the 6-K accession lookup + junction
writer are both idempotent) so this is safe to schedule on a cadence per
``directives/llm_quota_scheduling.md``.

Audit: each invocation writes an ``ingestion_runs`` row + per-(ticker, year,
quarter) ``stage_transitions`` rows (stage=COMPUTE), matching
``extract_segment_quarterly.py``'s wiring (Phase 1's 10-K-regime sibling).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from compute.segment_quarterly_6k import extract_for_ticker  # noqa: E402
from models.runs import StageName, StageStatus  # noqa: E402
from pipeline.invocation_fingerprint import payload_sha256  # noqa: E402
from pipeline.run_accounting import (  # noqa: E402
    JsonValue,
    PipelineRunSuppressedError,
    end_run,
    record_stage,
    start_run,
    suppression_payload,
)
from pipeline.source_routing import plan_for_ticker  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from table_extractors.period_axis import NominalQuarter  # noqa: E402

_QUARTERS: tuple[NominalQuarter, ...] = ("Q1", "Q2", "Q3")
_DEFAULT_LOOKBACK_QUARTERS = 4


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        print(f"[error] no DB at {db_path}", file=sys.stderr)
        return 1

    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row

    tickers = _resolve_tickers(conn, args)
    jobs = _resolve_jobs(conn, tickers, args)
    if not jobs:
        print("[]")
        conn.close()
        return 0

    try:
        run_id = start_run(
            conn,
            directive="extract_segment_quarterly_6k",
            ticker_scope=sorted({j[0] for j in jobs}),
            invocation_inputs=_invocation_inputs(jobs),
        )
    except PipelineRunSuppressedError as exc:
        print(json.dumps(suppression_payload(exc)))
        conn.close()
        return 0
    summary: list[dict[str, object]] = []
    final_status = StageStatus.OK
    error_summary: str | None = None

    try:
        for ticker, year, quarter, fye_month, fye_day in jobs:
            record_stage(conn, run_id, ticker, StageName.COMPUTE, StageStatus.IN_PROGRESS)
            try:
                result = extract_for_ticker(
                    ticker,
                    year,
                    quarter,
                    repo_root,
                    conn,
                    fye_month=fye_month,
                    fye_day=fye_day,
                )
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                record_stage(
                    conn, run_id, ticker, StageName.COMPUTE, StageStatus.FAILED, error_msg=err
                )
                summary.append({"ticker": ticker, "year": year, "quarter": quarter, "error": err})
                final_status = StageStatus.FAILED
                error_summary = err
                continue
            if result.skipped_reason is not None:
                record_stage(
                    conn,
                    run_id,
                    ticker,
                    StageName.COMPUTE,
                    StageStatus.SKIPPED,
                    error_msg=result.skipped_reason,
                )
            else:
                record_stage(conn, run_id, ticker, StageName.COMPUTE, StageStatus.OK)
            summary.append(
                {
                    "ticker": ticker,
                    "year": year,
                    "quarter": quarter,
                    "periods_inserted": result.periods_inserted,
                    "dimensions_inserted": result.dimensions_inserted,
                    "cells_skipped": result.cells_skipped,
                    "skipped": result.skipped_reason,
                }
            )
    finally:
        end_run(conn, run_id, final_status, error_summary=error_summary)
        conn.close()

    print(json.dumps(summary, indent=2))
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker")
    g.add_argument("--all", action="store_true", help="All fpi_6k-routed tracked tickers")
    p.add_argument("--year", type=int, default=None, help="Specific fiscal year")
    p.add_argument("--quarter", choices=["Q1", "Q2", "Q3"], default=None, help="Specific quarter")
    p.add_argument(
        "--lookback-quarters",
        type=int,
        default=_DEFAULT_LOOKBACK_QUARTERS,
        help="With --all and no explicit --year/--quarter: how many recent nominal "
        "quarters to attempt per ticker (default 4)",
    )
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    return p.parse_args()


def _resolve_tickers(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    cur = conn.cursor()
    cur.execute(
        f"SELECT DISTINCT ticker FROM tracked_companies "
        f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} ORDER BY ticker"
    )
    return [r[0] for r in cur.fetchall()]


def _fye_for_ticker(conn: sqlite3.Connection, ticker: str) -> tuple[int, int]:
    row = conn.execute(
        "SELECT fiscal_year_end FROM tracked_companies WHERE ticker = ?", (ticker,)
    ).fetchone()
    raw = row["fiscal_year_end"] if row is not None else None
    if isinstance(raw, str) and len(raw) == 5 and raw[2] == "-":
        try:
            return int(raw[:2]), int(raw[3:])
        except ValueError:
            pass
    return (12, 31)


def _recent_quarters(n: int) -> list[tuple[int, NominalQuarter]]:
    """Last n nominal (year, quarter) pairs, walking backward from the most
    recently fully-elapsed calendar quarter. Q4 has no distinct filing this
    module fetches (the fpi_6k route's Q4/annual handling is out of scope
    for Phase 3 -- bundled with the 20-F) -- stepped down to Q3 of the same
    year instead of ever emitting a "Q4" job."""
    today = date.today()
    year = today.year
    q_idx = (today.month - 1) // 3 - 1  # 0-3, most recently fully-elapsed quarter
    if q_idx < 0:
        year, q_idx = year - 1, 3

    out: list[tuple[int, NominalQuarter]] = []
    while len(out) < n:
        if q_idx == 3:
            q_idx = 2
            continue
        out.append((year, cast("NominalQuarter", f"Q{q_idx + 1}")))
        q_idx -= 1
        if q_idx < 0:
            year, q_idx = year - 1, 3
    return out


def _resolve_jobs(
    conn: sqlite3.Connection, tickers: list[str], args: argparse.Namespace
) -> list[tuple[str, int, NominalQuarter, int, int]]:
    jobs: list[tuple[str, int, NominalQuarter, int, int]] = []
    for ticker in tickers:
        try:
            plan = plan_for_ticker(conn, ticker)
        except ValueError:
            continue
        if plan.segment_quarterly_pipeline != "fpi_6k":
            sys.stderr.write(
                json.dumps(
                    {
                        "event": "segment_quarterly_6k_ticker_skipped",
                        "reason": "not_fpi_6k_regime",
                        "ticker": ticker,
                        "segment_quarterly_pipeline": plan.segment_quarterly_pipeline,
                    }
                )
                + "\n"
            )
            continue
        fye_month, fye_day = _fye_for_ticker(conn, ticker)
        if args.year is not None and args.quarter is not None:
            jobs.append(
                (ticker, args.year, cast("NominalQuarter", args.quarter), fye_month, fye_day)
            )
            continue
        for year, quarter in _recent_quarters(args.lookback_quarters):
            jobs.append((ticker, year, quarter, fye_month, fye_day))
    return jobs


def _invocation_inputs(
    jobs: list[tuple[str, int, NominalQuarter, int, int]],
) -> dict[str, JsonValue]:
    """Fingerprint the complete request; SEC bytes remain live and are not deduplicated."""
    job_payload: list[JsonValue] = [
        {
            "ticker": ticker,
            "year": year,
            "quarter": quarter,
            "fye_month": fye_month,
            "fye_day": fye_day,
        }
        for ticker, year, quarter, fye_month, fye_day in jobs
    ]
    return {
        "source": "live_sec_6k",
        "jobs": job_payload,
        "jobs_sha256": payload_sha256(job_payload),
    }


if __name__ == "__main__":
    raise SystemExit(main())
