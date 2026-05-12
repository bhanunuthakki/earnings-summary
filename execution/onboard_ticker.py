"""Onboard a newly-tracked ticker: fetch FMP fundamentals, then run parse stages.

Bridges the gap between `db.track_company` (a snappy DB upsert) and the rest of
the pipeline (network-bound fetches + parse). Designed to be invoked as a
fire-and-forget subprocess from `db.track_company` when a ticker is added to
the portfolio, watchlist, or evaluation list (`db.ACTIVE_LIST_TYPES`) — see
the call site there.

Sequence per ticker:
    1. FMP fetch via `execution/save_fmp_data.py --tickers TICKER --skip-existing`
       (subprocess; the script has its own DB-connection lifecycle and resumes
       cleanly via fmp_endpoint_status).
    2. Quarterly refresh via `pipeline.quarterly_refresh.refresh_ticker`
       (in-process; idempotent across all 7 stages).
    3. Transcript backfill via `execution/backfill_transcripts.py --ticker X`
       (subprocess; fetches the last 6 fiscal quarters of Q&A from the free
       aggregator chain, runs ingest, and extracts forward-looking
       commitments). Tolerates aggregator misses — they don't fail the
       onboard. The daily cron `cron/backfill_transcripts.task.xml` covers
       the same ground for the full active universe on a daily cadence.

Usage:
    python execution/onboard_ticker.py --ticker BKNG
    python execution/onboard_ticker.py --ticker BKNG --skip-fmp           # parse-only
    python execution/onboard_ticker.py --ticker BKNG --skip-transcripts   # no aggregator fetch

This is the FMP + aggregator-transcript onboard path. SEC XBRL, IR-doc, and
audio fallback fetches stay explicit user actions per
`directives/data_pipeline_dag.md`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.runs import StageStatus as RunStageStatus  # noqa: E402
from pipeline.fmp_doc_index import (  # noqa: E402
    index_fmp_files_for_ticker,
    set_fiscal_year_end_from_fmp,
)
from pipeline.quarterly_refresh import (  # noqa: E402
    StageStatus as RefreshStageStatus,
)
from pipeline.quarterly_refresh import refresh_ticker  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from pipeline.run_accounting import end_run, start_run  # noqa: E402

_HOLDINGS_DIR = PROJECT_ROOT / "micro_thesis" / "holdings"
_DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"
_FMP_SCRIPT = PROJECT_ROOT / "execution" / "save_fmp_data.py"
_BACKFILL_SCRIPT = PROJECT_ROOT / "execution" / "backfill_transcripts.py"


def _run_fmp_fetch(ticker: str) -> int:
    """Invoke save_fmp_data.py as a subprocess; return its exit code."""
    cmd = [
        sys.executable,
        str(_FMP_SCRIPT),
        "--tickers", ticker,
        "--skip-existing",
    ]
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    return proc.returncode


def _run_transcript_backfill(ticker: str) -> int:
    """Invoke backfill_transcripts.py for a single ticker.

    Fetches recent Q&A transcripts from free aggregators, ingests them, and
    extracts commitments. Tolerates aggregator coverage gaps.
    """
    cmd = [
        sys.executable,
        str(_BACKFILL_SCRIPT),
        "--ticker", ticker,
    ]
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", required=True, help="Ticker to onboard (e.g. BKNG)")
    ap.add_argument(
        "--skip-fmp", action="store_true",
        help="Skip the FMP fetch step (parse-only; useful for re-runs)",
    )
    ap.add_argument(
        "--skip-transcripts", action="store_true",
        help="Skip the aggregator-transcript backfill step (faster onboard for re-runs)",
    )
    args = ap.parse_args()
    ticker = args.ticker.upper()

    started = datetime.now()
    print(f"[onboard] {ticker} starting at {started.isoformat(timespec='seconds')}", flush=True)

    if not args.skip_fmp:
        print(f"[onboard] {ticker} stage=fmp_fetch", flush=True)
        rc = _run_fmp_fetch(ticker)
        if rc != 0:
            print(f"[onboard] {ticker} fmp_fetch FAILED (rc={rc}); continuing to parse", flush=True)

    conn = open_db(_DB_PATH)
    try:
        print(f"[onboard] {ticker} stage=index_fmp_documents", flush=True)
        n_indexed = index_fmp_files_for_ticker(conn, ticker, PROJECT_ROOT)
        print(f"[onboard] {ticker} indexed {n_indexed} new fmp documents rows", flush=True)

        print(f"[onboard] {ticker} stage=set_fiscal_year_end", flush=True)
        fye = set_fiscal_year_end_from_fmp(conn, ticker, PROJECT_ROOT)
        print(f"[onboard] {ticker} fiscal_year_end={fye!s}", flush=True)

        print(f"[onboard] {ticker} stage=quarterly_refresh", flush=True)
        run_id = start_run(conn, directive="onboard_ticker", ticker_scope=[ticker])
        report = refresh_ticker(
            conn,
            ticker=ticker,
            project_root=PROJECT_ROOT,
            holdings_dir=_HOLDINGS_DIR,
            run_id=run_id,
            fetch_sec=False,
        )
        any_failed = any(s.status is RefreshStageStatus.FAILED for s in report.stages)
        end_run(
            conn, run_id,
            RunStageStatus.OK if not any_failed else RunStageStatus.FAILED,
            error_summary="one or more stages failed" if any_failed else None,
        )
        for s in report.stages:
            print(f"[onboard] {ticker} {s.name.value:24s} {s.status.value:8s} rows={s.rows_processed:<5} {s.notes}", flush=True)
    finally:
        conn.close()

    transcript_rc: int | None = None
    if not args.skip_transcripts:
        print(f"[onboard] {ticker} stage=backfill_transcripts", flush=True)
        transcript_rc = _run_transcript_backfill(ticker)
        if transcript_rc != 0:
            # Aggregator gaps are expected; log but don't fail the onboard.
            print(
                f"[onboard] {ticker} backfill_transcripts returned rc={transcript_rc}; "
                f"continuing (aggregator misses are tolerated)",
                flush=True,
            )

    elapsed = (datetime.now() - started).total_seconds()
    print(
        f"[onboard] {ticker} done in {elapsed:.1f}s; "
        f"failed_stages={any_failed}; transcript_rc={transcript_rc}",
        flush=True,
    )
    return 0 if not any_failed else 1


if __name__ == "__main__":
    sys.exit(main())
