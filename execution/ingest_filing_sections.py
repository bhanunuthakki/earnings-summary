"""Ingest filing sections into the ``filing_sections`` partition store.

Layer-3 entrypoint for docs/design/filing_longitudinal_language.md Phase 0.
Three independent lanes, selectable with ``--sources``:

  fmp       cached financial-reports-json payloads (no network)
  edgar     EDGAR primary-document narrative, by accession (network)
  exhibits  already-downloaded 6-K exhibits registered in ``documents``

Every lane is idempotent: re-running re-writes the same rows, so a partial run
resumes simply by running again. A lane that cannot run records honest
coverage rather than failing the ticker — the common state on this book is
exactly one lane being available for a given name.

Structured events go to stderr, one JSON object per line; a machine-readable
run summary goes to stdout. Exit codes: 0 success (including recorded
degradations), 1 hard stop (missing migration, auth failure, dangling
provenance pointer), 2 bad arguments.

Usage:
    python execution/ingest_filing_sections.py --tickers META --sources fmp
    python execution/ingest_filing_sections.py --all-portfolio --sources fmp,exhibits
    python execution/ingest_filing_sections.py --tickers NU --sources edgar --limit-per-form 5
    python execution/ingest_filing_sections.py --tickers META --sources edgar --offline
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from filings import ingest, store  # noqa: E402
from filings.models import FilingForm, HardStopError  # noqa: E402

_VALID_SOURCES = ("fmp", "edgar", "exhibits")
_EXIT_HARD_STOP = 1
_EXIT_BAD_ARGS = 2
_PACIFIC = ZoneInfo("America/Los_Angeles")


class _Completed(Protocol):
    returncode: int
    stderr: str


class _Runner(Protocol):
    def __call__(self, argv: list[str], **kwargs: object) -> _Completed: ...


_SUBPROCESS_RUNNER = cast("_Runner", subprocess.run)


def _accessions_for_ticker(conn: sqlite3.Connection, ticker: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT accession_number
        FROM filing_sections
        WHERE ticker = ? AND accession_number IS NOT NULL
          AND TRIM(accession_number) != ''
        """,
        (ticker.upper(),),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _trigger_disclosure_fast_path(
    *,
    tickers: tuple[str, ...],
    db_path: Path,
    project_root: Path = PROJECT_ROOT,
    now: datetime | None = None,
    runner: _Runner = _SUBPROCESS_RUNNER,
) -> Literal["not_needed", "deferred_protected_window", "complete", "failed"]:
    """Run D3 for newly ingested accessions, clear of protected LLM hours."""

    if not tickers:
        return "not_needed"
    local_now = (now or datetime.now(_PACIFIC)).astimezone(_PACIFIC)
    if 3 <= local_now.hour < 5:
        return "deferred_protected_window"
    argv = [
        sys.executable,
        str(project_root / "execution" / "run_disclosure_change_sweep.py"),
        "--tickers",
        ",".join(sorted(set(tickers))),
        "--db-path",
        str(db_path),
    ]
    proc = runner(
        argv,
        cwd=str(project_root),
        capture_output=True,
        text=True,
        check=False,
    )
    return "complete" if proc.returncode == 0 else "failed"


class _JsonLineFormatter(logging.Formatter):
    """One JSON object per stderr line — never mixed with stdout data."""

    def format(self, record: logging.LogRecord) -> str:
        # `LogRecord.msg` is untyped; this package logs dict payloads, and
        # anything else (a third-party library logging a string) is wrapped.
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


def _portfolio_tickers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT ticker FROM tracked_companies WHERE list_type = 'portfolio' "
        "AND COALESCE(instrument_type, '') != 'etf' ORDER BY ticker"
    ).fetchall()
    return [str(r[0]).upper() for r in rows]


def _parse_forms(raw: str | None) -> frozenset[FilingForm]:
    if not raw:
        return ingest.DEFAULT_EDGAR_FORMS
    out: set[FilingForm] = set()
    for token in raw.split(","):
        token = token.strip().upper()
        if not token:
            continue
        try:
            out.add(FilingForm(token))
        except ValueError as exc:
            raise SystemExit(
                f"unknown form {token!r}; valid: {[f.value for f in FilingForm]}"
            ) from exc
    return frozenset(out)


def _parse_years(raw: str | None) -> set[int] | None:
    if not raw:
        return None
    out: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start, _, end = token.partition("-")
            out.update(range(int(start), int(end) + 1))
        else:
            out.add(int(token))
    return out or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tickers", type=str, default=None, help="Comma-separated tickers")
    parser.add_argument("--all-portfolio", action="store_true", help="Every portfolio ticker")
    parser.add_argument(
        "--sources",
        type=str,
        default="fmp",
        help=f"Comma-separated lanes to run: {','.join(_VALID_SOURCES)} (default: fmp)",
    )
    parser.add_argument(
        "--years", type=str, default=None, help="e.g. 2021-2025 or 2023,2025 (fmp lane)"
    )
    parser.add_argument("--forms", type=str, default=None, help="e.g. 10-K,20-F (edgar lane)")
    parser.add_argument(
        "--limit-per-form", type=int, default=6, help="Accessions per form (edgar lane)"
    )
    parser.add_argument(
        "--offline", action="store_true", help="edgar lane: cache only, never fetch"
    )
    parser.add_argument("--user-agent", type=str, default=None, help="SEC User-Agent override")
    parser.add_argument("--db-path", type=str, default=None, help="Portfolio DB path override")
    parser.add_argument(
        "--reconcile", action="store_true", help="Run cross-source reconciliation after ingest"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    log = logging.getLogger("ingest_filing_sections")

    lanes = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    unknown = [lane for lane in lanes if lane not in _VALID_SOURCES]
    if unknown:
        log.error({"event": "bad_sources", "unknown": unknown, "valid": list(_VALID_SOURCES)})
        return _EXIT_BAD_ARGS
    if not lanes:
        log.error({"event": "no_sources_selected"})
        return _EXIT_BAD_ARGS

    if args.db_path:
        db.set_db_path(args.db_path)
    conn = db.get_connection()

    # Data files (cached FMP payloads, 6-K exhibit HTML, documents.file_path
    # values) live alongside the DB, not alongside the code — running this from
    # a worktree against the main checkout's DB is the normal case, and
    # resolving paths from PROJECT_ROOT there would look in an empty tree.
    data_root = Path(db.DATA_DIR).parent

    try:
        if args.all_portfolio:
            tickers = _portfolio_tickers(conn)
        elif args.tickers:
            tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        else:
            log.error({"event": "no_tickers", "hint": "pass --tickers or --all-portfolio"})
            return _EXIT_BAD_ARGS
        if not tickers:
            log.error({"event": "empty_ticker_set"})
            return _EXIT_BAD_ARGS

        try:
            forms = _parse_forms(args.forms)
            years = _parse_years(args.years)
        except (ValueError, SystemExit) as exc:
            log.error({"event": "bad_arguments", "error": str(exc)})
            return _EXIT_BAD_ARGS

        # Fail fast on a missing migration rather than per-ticker: it is a
        # setup error, and reporting it once is clearer than N times.
        try:
            store.get_sections(conn, tickers[0], limit=1)
        except HardStopError as exc:
            log.error({"event": "hard_stop", "stage": "preflight", "error": str(exc)})
            return _EXIT_HARD_STOP

        # Reports are kept as typed objects and only serialized at the end, so
        # the aggregation below reads real ints instead of re-widening
        # everything to `object` and casting it back.
        reports: list[tuple[str, ingest.IngestReport]] = []
        findings: dict[str, list[str]] = {}
        new_accession_tickers: list[str] = []
        started = datetime.now(UTC).replace(tzinfo=None)

        for ticker in tickers:
            accessions_before = _accessions_for_ticker(conn, ticker)
            for lane in lanes:
                try:
                    if lane == "fmp":
                        report = ingest.ingest_fmp(conn, ticker, data_root, years=years)
                    elif lane == "edgar":
                        report = ingest.ingest_edgar(
                            conn,
                            ticker,
                            data_root,
                            forms=forms,
                            limit_per_form=args.limit_per_form,
                            offline=args.offline,
                            **({"user_agent": args.user_agent} if args.user_agent else {}),
                        )
                    else:
                        report = ingest.ingest_local_exhibits(conn, ticker, data_root)
                except HardStopError as exc:
                    conn.commit()
                    log.error(
                        {"event": "hard_stop", "ticker": ticker, "lane": lane, "error": str(exc)}
                    )
                    return _EXIT_HARD_STOP
                conn.commit()
                reports.append((lane, report))
                log.info({"event": "lane_complete", "lane": lane, **report.as_dict()})

            if args.reconcile:
                try:
                    ticker_findings = ingest.reconcile_sources(conn, ticker)
                except HardStopError as exc:
                    log.error(
                        {
                            "event": "hard_stop",
                            "ticker": ticker,
                            "stage": "reconcile",
                            "error": str(exc),
                        }
                    )
                    return _EXIT_HARD_STOP
                conn.commit()
                if ticker_findings:
                    findings[ticker] = ticker_findings
                    log.warning(
                        {
                            "event": "cross_source_mismatch",
                            "ticker": ticker,
                            "findings": ticker_findings,
                        }
                    )

            if _accessions_for_ticker(conn, ticker) - accessions_before:
                new_accession_tickers.append(ticker)

        fast_path_status = _trigger_disclosure_fast_path(
            tickers=tuple(new_accession_tickers),
            db_path=Path(db.DB_PATH),
        )
        if fast_path_status == "deferred_protected_window":
            log.info(
                {
                    "event": "disclosure_fast_path_deferred",
                    "reason": "protected_llm_window",
                    "tickers": new_accession_tickers,
                }
            )
        elif fast_path_status == "failed":
            log.error(
                {
                    "event": "disclosure_fast_path_failed",
                    "tickers": new_accession_tickers,
                    "hint": "weekly sweep will retry because the checkpoint was not advanced",
                }
            )

        summary: dict[str, object] = {
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "tickers": tickers,
            "lanes": lanes,
            "sections_written": sum(r.sections_written for _, r in reports),
            "documents_ingested": sum(r.documents_ingested for _, r in reports),
            "documents_seen": sum(r.documents_seen for _, r in reports),
            "coverage_rows": sum(r.coverage_rows for _, r in reports),
            "failures": [f for _, r in reports for f in r.failures],
            "mismatches": [m for _, r in reports for m in r.mismatches],
            "cross_source_findings": findings,
            "new_accession_tickers": new_accession_tickers,
            "disclosure_fast_path": fast_path_status,
            "reports": [{"lane": lane, **r.as_dict()} for lane, r in reports],
        }
        json.dump(summary, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return _EXIT_HARD_STOP if fast_path_status == "failed" else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
