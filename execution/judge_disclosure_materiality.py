"""Judge disclosure-drift events against the thesis: may they ELEVATE?

Layer-3 entrypoint for the thesis-materiality elevation gate (owner ruling
2026-08-02: raw disclosure drift is too onerous — an event may reach an
owner-facing surface only when an LLM judges that it fundamentally restricts
the ability to MEASURE the thesis). For each ticker: load the thesis anchor
(micro_thesis/holdings/<T>.json via llm.anchors), fetch unjudged
elevation-eligible events, batch them to the ``disclosure_thesis_materiality``
purpose, and persist ``thesis_materiality`` / ``thesis_materiality_rationale``
/ ``thesis_materiality_judged_at`` back onto ``disclosure_events``
(migration 0271).

Degrade discipline (per-item pattern, directives/llm_quota_scheduling.md): a
ticker with no thesis on file is SKIPPED loudly (rows stay NULL = not
elevated); a transient LLM failure leaves its batch NULL, is tallied in the
summary, and retries on the next weekly sweep run; hard stops (budget/setup)
exit non-zero. Structured events go to stderr, one JSON object per line; a
machine-readable run summary goes to stdout. Exit codes: 0 success, 1 hard
stop, 2 bad arguments.

Usage:
    python execution/judge_disclosure_materiality.py --tickers NU
    python execution/judge_disclosure_materiality.py --all-portfolio
    python execution/judge_disclosure_materiality.py --tickers WIX --dry-run --verbose
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
from filings import materiality_judgment as mj  # noqa: E402
from filings.models import HardStopError  # noqa: E402
from llm.anchors import load_thesis_anchor  # noqa: E402
from llm.cli import is_hard_stop  # noqa: E402

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


def _portfolio_tickers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT ticker FROM tracked_companies WHERE list_type = 'portfolio' "
        "AND COALESCE(instrument_type, '') != 'etf' ORDER BY ticker"
    ).fetchall()
    return [str(r[0]).upper() for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tickers", type=str, default=None, help="Comma-separated tickers")
    parser.add_argument("--all-portfolio", action="store_true", help="Every portfolio ticker")
    parser.add_argument("--db-path", type=str, default=None, help="Portfolio DB path override")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Root holding micro_thesis/holdings/ (thesis anchors)",
    )
    parser.add_argument(
        "--rejudge",
        action="store_true",
        help="Re-judge events that already carry a thesis_materiality verdict",
    )
    parser.add_argument(
        "--max-per-ticker",
        type=int,
        default=150,
        help="Cap on events judged per ticker per run (backlog drains across runs)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Judge but write nothing to the DB")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    log = logging.getLogger("judge_disclosure_materiality")

    if args.db_path:
        db.set_db_path(args.db_path)
    conn = db.get_connection()

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
            mj.fetch_judgment_candidates(conn, tickers[0], only_unjudged=False, limit=1)
        except HardStopError as exc:
            log.error({"event": "hard_stop", "stage": "preflight", "error": str(exc)})
            return _EXIT_HARD_STOP
        except sqlite3.OperationalError as exc:
            # Missing thesis_materiality column — migration 0271 not applied.
            log.error({"event": "hard_stop", "stage": "preflight", "error": str(exc)})
            return _EXIT_HARD_STOP

        started = datetime.now(UTC).replace(tzinfo=None)
        totals = {
            "events_judged": 0,
            "restricts_measurement": 0,
            "not_material": 0,
            "deferred_tabular": 0,
        }
        per_ticker: list[dict[str, object]] = []
        degraded_tickers: list[str] = []
        skipped_no_thesis: list[str] = []

        for ticker in tickers:
            candidates = mj.fetch_judgment_candidates(
                conn,
                ticker,
                only_unjudged=not args.rejudge,
                limit=args.max_per_ticker,
            )
            if not candidates:
                per_ticker.append({"ticker": ticker, "events_judged": 0})
                continue
            anchor = load_thesis_anchor(args.repo_root, ticker)
            try:
                outcome = mj.judge_ticker_events(
                    ticker,
                    candidates,
                    anchor,
                    db_path=args.db_path,
                )
            except Exception as exc:
                if is_hard_stop(exc):
                    log.error({"event": "hard_stop", "ticker": ticker, "error": str(exc)})
                    return _EXIT_HARD_STOP
                raise

            if outcome.skipped_no_thesis:
                skipped_no_thesis.append(ticker)
            if outcome.degraded:
                degraded_tickers.append(ticker)

            verdicts = list(outcome.verdicts.values())
            if not args.dry_run and verdicts:
                mj.write_judgments(conn, verdicts)
                conn.commit()

            totals["events_judged"] += len(verdicts)
            totals["deferred_tabular"] += outcome.deferred_tabular
            for v in verdicts:
                totals[v.materiality.value] = totals.get(v.materiality.value, 0) + 1

            per_ticker.append(
                {
                    "ticker": ticker,
                    "candidates": len(candidates),
                    "events_judged": len(verdicts),
                    "restricts_measurement": sum(
                        1
                        for v in verdicts
                        if v.materiality is mj.ThesisMateriality.RESTRICTS_MEASUREMENT
                    ),
                    "deferred_tabular": outcome.deferred_tabular,
                    "skipped_no_thesis": outcome.skipped_no_thesis,
                    "llm_degraded": outcome.degraded,
                }
            )
            log.info(
                {
                    "event": "ticker_judged",
                    "ticker": ticker,
                    "candidates": len(candidates),
                    "judged": len(verdicts),
                    "skipped_no_thesis": outcome.skipped_no_thesis,
                    "llm_degraded": outcome.degraded,
                }
            )

        summary: dict[str, object] = {
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "tickers": tickers,
            "rejudge": args.rejudge,
            "dry_run": args.dry_run,
            "totals": totals,
            "degraded_tickers": degraded_tickers,
            "skipped_no_thesis": skipped_no_thesis,
            "per_ticker": per_ticker,
        }
        json.dump(summary, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
