"""Classify item-level disclosure changes as boilerplate_update vs substantive.

Layer-3 entrypoint for the P3 build (docs/design/disclosure_change_build_stack.md
§P3). For each ticker: fetch its unclassified item_added/item_removed/
item_reworded events, resolve the confidently-boilerplate and confidently-
substantive extremes deterministically (filings.specificity), batch the
ambiguous survivors into one LLM call per ticker plus at most one missing-id
recovery call (filings.boilerplate_triage), and persist
verdict/confidence/interpretation_md back onto disclosure_events
(filings.boilerplate_classify).

A degraded or missing LLM verdict leaves the row `unclassified` — never a
fabricated `boilerplate_update`. Structured events go to stderr, one JSON
object per line; a machine-readable run summary goes to stdout. Exit codes:
0 success, 1 hard stop (missing migration), 2 bad arguments.

Usage:
    python execution/classify_disclosure_specificity.py --tickers META
    python execution/classify_disclosure_specificity.py --all-portfolio
    python execution/classify_disclosure_specificity.py --tickers WIX --dry-run --verbose
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
from filings import boilerplate_classify as bc  # noqa: E402
from filings.models import HardStopError  # noqa: E402

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
        "--reclassify",
        action="store_true",
        help="Re-classify events that already carry a verdict (default: unclassified only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute classifications but write nothing to the DB",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    log = logging.getLogger("classify_disclosure_specificity")

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
            bc.fetch_item_events(conn, tickers[0], only_unclassified=False)
        except HardStopError as exc:
            log.error({"event": "hard_stop", "stage": "preflight", "error": str(exc)})
            return _EXIT_HARD_STOP

        started = datetime.now(UTC).replace(tzinfo=None)
        # Risk-growth (Filzen 2015) flags computed ONCE per run across the
        # requested tickers, then reused per ticker.
        risk_growth_flags = bc.compute_risk_growth_flags(conn, tickers)

        totals = {
            "events_classified": 0,
            "boilerplate_update": 0,
            "substantive": 0,
            "unclassified": 0,
            "deterministic": 0,
            "llm_used": 0,
        }
        per_ticker: list[dict[str, object]] = []
        degraded_tickers: list[str] = []

        for ticker in tickers:
            results, llm_degraded = bc.classify_ticker_events(
                conn,
                ticker,
                risk_growth_flags=risk_growth_flags,
                only_unclassified=not args.reclassify,
            )
            if llm_degraded:
                degraded_tickers.append(ticker)

            if not args.dry_run and results:
                bc.write_classifications(conn, results)
                conn.commit()

            for r in results:
                totals["events_classified"] += 1
                totals[r.verdict] = totals.get(r.verdict, 0) + 1
                totals["llm_used" if r.llm_used else "deterministic"] += 1

            per_ticker.append(
                {
                    "ticker": ticker,
                    "events_classified": len(results),
                    "llm_degraded": llm_degraded,
                    "by_verdict": {
                        v: sum(1 for r in results if r.verdict == v)
                        for v in (
                            bc.BOILERPLATE_VERDICT,
                            bc.SUBSTANTIVE_VERDICT,
                            bc.UNCLASSIFIED_VERDICT,
                        )
                    },
                }
            )
            log.info(
                {
                    "event": "ticker_classified",
                    "ticker": ticker,
                    "events": len(results),
                    "llm_degraded": llm_degraded,
                }
            )

        summary: dict[str, object] = {
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "tickers": tickers,
            "reclassify": args.reclassify,
            "dry_run": args.dry_run,
            "totals": totals,
            "degraded_tickers": degraded_tickers,
            "per_ticker": per_ticker,
        }
        json.dump(summary, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
