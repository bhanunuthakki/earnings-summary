"""Comprehensive quarterly refresh orchestrator (cron entry point).

Runs the full per-ticker DAG idempotently: extract FMP facts, ingest IR
transcripts, derive KPIs, match outstanding commitments, re-evaluate thesis,
and surface any LLM-needed work for a follow-up session.

Usage:
    python execution/quarterly_refresh.py                  # all tracked tickers
    python execution/quarterly_refresh.py --ticker MELI    # single ticker
    python execution/quarterly_refresh.py --json           # full machine output
    python execution/quarterly_refresh.py --dry-run        # parse args, show plan only

Recommended cron (Linux):
    # 5th of every month + 1 week after typical earnings dates
    0 6 5 * *  cd /path/to/earnings-summary && venv/bin/python execution/quarterly_refresh.py >> logs/refresh.log 2>&1

Recommended Task Scheduler (Windows):
    schtasks /Create /TN "EarningsRefresh" /TR "C:\\path\\venv\\Scripts\\python.exe C:\\path\\execution\\quarterly_refresh.py" /SC MONTHLY /D 5 /ST 06:00
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.runs import StageStatus as RunStageStatus  # noqa: E402
from pipeline.quarterly_refresh import (  # noqa: E402
    RefreshReport,
    TickerRefreshReport,
    refresh_portfolio,
)
from pipeline.quarterly_refresh import StageStatus as RefreshStageStatus  # noqa: E402
from pipeline.queries import open_db, tracked_companies_for_user  # noqa: E402
from pipeline.run_accounting import end_run, start_run  # noqa: E402

_HOLDINGS_DIR = PROJECT_ROOT / "micro_thesis" / "holdings"


def _resolve_tickers(conn, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    companies = tracked_companies_for_user(conn, only_classified=True)
    return [c.ticker for c in companies]


def _report_to_dict(report: RefreshReport) -> dict[str, object]:
    return {
        "run_id": report.run_id,
        "started_at": report.started_at.isoformat(),
        "ended_at": report.ended_at.isoformat(),
        "tickers": [_ticker_to_dict(t) for t in report.tickers],
    }


def _ticker_to_dict(t: TickerRefreshReport) -> dict[str, object]:
    return {
        "ticker": t.ticker,
        "breach_status": t.breach_status.value if t.breach_status is not None else None,
        "breach_status_changed": t.breach_status_changed,
        "stages": [
            {
                "name": s.name.value,
                "status": s.status.value,
                "rows_processed": s.rows_processed,
                "notes": s.notes,
            }
            for s in t.stages
        ],
        "pending_work": [
            {
                "kind": p.kind,
                "document_id": p.document_id,
                "transcript_id": p.transcript_id,
                "file_path": p.file_path,
                "period_end": p.period_end.date().isoformat() if p.period_end else None,
                "note": p.note,
            }
            for p in t.pending_work
        ],
    }


def _print_summary_table(report: RefreshReport) -> None:
    """Compact human-readable summary printed to stdout (machine JSON via --json)."""
    print(f"run_id: {report.run_id}")
    print(f"elapsed: {(report.ended_at - report.started_at).total_seconds():.2f}s")
    print()
    header = (
        f"{'Ticker':6}  {'Status':6}  {'Chg':3}  "
        f"{'Facts':>6}  {'Trnsc':>6}  {'Drv':>4}  {'Cmt':>4}  {'Sig':>4}  Pending"
    )
    print(header)
    print("-" * len(header))
    for t in report.tickers:
        by_name = {s.name.value: s for s in t.stages}
        facts = by_name["extract_fmp_facts"].rows_processed
        trnsc = by_name["ingest_ir_transcripts"].rows_processed
        drv = by_name["derive_fmp_kpis"].rows_processed
        cmt = by_name["match_commitments"].rows_processed
        sig_stage = by_name.get("persist_timeseries_signals")
        sig = sig_stage.rows_processed if sig_stage is not None else 0
        pending = by_name["surface_pending_llm"].rows_processed
        sec = by_name.get("fetch_sec_xbrl")
        if sec is not None and sec.rows_processed:
            # Only print SEC counts when the stage actually ran.
            facts = facts + sec.rows_processed
        status_str = t.breach_status.value if t.breach_status is not None else "-"
        change_str = "*" if t.breach_status_changed else ""
        print(
            f"{t.ticker:6}  {status_str:6}  {change_str:3}  "
            f"{facts:>6}  {trnsc:>6}  {drv:>4}  {cmt:>4}  {sig:>4}  {pending}"
        )

    changed = [t for t in report.tickers if t.breach_status_changed]
    if changed:
        print()
        print("Status transitions this run:")
        for t in changed:
            change_msg = next(
                (s.notes for s in t.stages if s.name.value == "evaluate_thesis"),
                "",
            )
            print(f"  {t.ticker:6}  {change_msg}")

    cache_flagged = [
        t
        for t in report.tickers
        if any(
            s.name.value == "validate_segment_cache" and s.status is RefreshStageStatus.FAILED
            for s in t.stages
        )
    ]
    if cache_flagged:
        print()
        print(
            "Segment-cache contamination (FMP) - ingest gate drops these; raw cache needs re-fetch/fix:"
        )
        for t in cache_flagged:
            note = next((s.notes for s in t.stages if s.name.value == "validate_segment_cache"), "")
            print(f"  {t.ticker:6}  {note}")

    total_pending = sum(len(t.pending_work) for t in report.tickers)
    if total_pending:
        print()
        print(
            f"{total_pending} item(s) pending LLM follow-up "
            f"(IR PDFs + transcripts). Run with --json for details."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="Restrict to a single ticker")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full report as JSON (default: human-readable table)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned ticker scope and exit; make no DB changes",
    )
    parser.add_argument(
        "--fetch-sec",
        action="store_true",
        help="Opt-in: hit SEC companyfacts API at the start of each ticker's DAG. "
        "Default off so the cron stays network-free.",
    )
    parser.add_argument(
        "--db",
        default=str(PROJECT_ROOT / "data" / "portfolio.db"),
    )
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        tickers = _resolve_tickers(conn, args)
        if args.dry_run:
            print(json.dumps({"plan": {"tickers": tickers}}, indent=2))
            return 0
        if not tickers:
            print(json.dumps({"warning": "no tickers resolved"}, indent=2))
            return 0

        run_id = start_run(
            conn,
            directive="quarterly_refresh",
            ticker_scope=tickers,
            invocation_inputs={"fetch_sec": bool(args.fetch_sec)},
        )
        report = refresh_portfolio(
            conn,
            tickers=tickers,
            project_root=PROJECT_ROOT,
            holdings_dir=_HOLDINGS_DIR,
            run_id=run_id,
            fetch_sec=args.fetch_sec,
        )
        any_failed = any(
            stage.status is RefreshStageStatus.FAILED for t in report.tickers for stage in t.stages
        )
        end_run(
            conn,
            run_id,
            RunStageStatus.OK if not any_failed else RunStageStatus.FAILED,
            error_summary="one or more stages failed" if any_failed else None,
        )

        if args.json:
            print(json.dumps(_report_to_dict(report), indent=2))
        else:
            _print_summary_table(report)
        return 0 if not any_failed else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
