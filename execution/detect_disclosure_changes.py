"""Detect item-level disclosure changes: additions, removals, rewordings.

Layer-3 entrypoint for the P0 build (docs/design/disclosure_change_build_stack.md
§P0). For each (ticker, canonical concept) pair: split every stored
``filing_sections`` period into atomic items (``filings.section_items``),
persist them, then align consecutive periods and classify what changed
(``filings.item_diff``). Zero LLM — this is deterministic text alignment only.

Structured events go to stderr, one JSON object per line; a machine-readable
run summary goes to stdout. Exit codes: 0 success (including tickers with
too little history to diff), 1 hard stop (missing migration, dangling
provenance pointer), 2 bad arguments.

Usage:
    python execution/detect_disclosure_changes.py --tickers META --concepts risk_factors,mdna
    python execution/detect_disclosure_changes.py --all-portfolio --concepts risk_factors
    python execution/detect_disclosure_changes.py --tickers WIX --dry-run --verbose
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
from filings import item_diff, section_items, store  # noqa: E402
from filings.models import HardStopError  # noqa: E402

_DEFAULT_CONCEPTS = ("risk_factors", "mdna")
_EXIT_HARD_STOP = 1
_EXIT_BAD_ARGS = 2


class _JsonLineFormatter(logging.Formatter):
    """One JSON object per stderr line — never mixed with stdout data."""

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


def _parse_concepts(raw: str | None) -> list[str]:
    if not raw:
        return list(_DEFAULT_CONCEPTS)
    out = [token.strip() for token in raw.split(",") if token.strip()]
    return out or list(_DEFAULT_CONCEPTS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tickers", type=str, default=None, help="Comma-separated tickers")
    parser.add_argument("--all-portfolio", action="store_true", help="Every portfolio ticker")
    parser.add_argument(
        "--concepts",
        type=str,
        default=None,
        help=f"Comma-separated canonical_id concepts (default: {','.join(_DEFAULT_CONCEPTS)})",
    )
    parser.add_argument("--db-path", type=str, default=None, help="Portfolio DB path override")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute items/events but write nothing to the DB",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    log = logging.getLogger("detect_disclosure_changes")

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

        concepts = _parse_concepts(args.concepts)

        # Fail fast on a missing migration rather than per-ticker: it is a
        # setup error, and reporting it once is clearer than N times.
        try:
            store.get_sections(conn, tickers[0], limit=1)
            section_items.get_items(conn, tickers[0])
            item_diff.get_events(conn, tickers[0])
        except HardStopError as exc:
            log.error({"event": "hard_stop", "stage": "preflight", "error": str(exc)})
            return _EXIT_HARD_STOP

        started = datetime.now(UTC).replace(tzinfo=None)
        per_ticker: list[dict[str, object]] = []
        totals = {
            "items_written": 0,
            "events_written": 0,
            "item_added": 0,
            "item_removed": 0,
            "item_reworded": 0,
        }
        insufficient_history: list[str] = []

        for ticker in tickers:
            ticker_items_written = 0
            ticker_events: list[item_diff.DisclosureEvent] = []
            for concept in concepts:
                try:
                    items, events = item_diff.diff_ticker_concept(conn, ticker, concept)
                except HardStopError as exc:
                    conn.commit()
                    log.error(
                        {
                            "event": "hard_stop",
                            "ticker": ticker,
                            "concept": concept,
                            "error": str(exc),
                        }
                    )
                    return _EXIT_HARD_STOP

                if not items:
                    log.info({"event": "no_sections", "ticker": ticker, "concept": concept})
                    continue

                if not args.dry_run:
                    section_items.write_items(conn, items)
                    conn.commit()
                ticker_items_written += len(items)

                if not events:
                    log.info(
                        {
                            "event": "no_events",
                            "ticker": ticker,
                            "concept": concept,
                            "items_split": len(items),
                        }
                    )
                    if len({(i.fiscal_year, i.fiscal_period.value) for i in items}) < 2:
                        insufficient_history.append(f"{ticker}:{concept}")
                    continue

                ticker_events.extend(events)
                log.info(
                    {
                        "event": "concept_diffed",
                        "ticker": ticker,
                        "concept": concept,
                        "items_split": len(items),
                        "events": len(events),
                    }
                )

            if not args.dry_run and ticker_events:
                item_diff.write_events(conn, ticker_events)
                conn.commit()

            for e in ticker_events:
                totals[e.event_type] = totals.get(e.event_type, 0) + 1
            totals["items_written"] += ticker_items_written
            totals["events_written"] += len(ticker_events)

            per_ticker.append(
                {
                    "ticker": ticker,
                    "items_written": ticker_items_written,
                    "events": len(ticker_events),
                    "by_type": {
                        et: sum(1 for e in ticker_events if e.event_type == et)
                        for et in ("item_added", "item_removed", "item_reworded")
                    },
                }
            )

        summary: dict[str, object] = {
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "tickers": tickers,
            "concepts": concepts,
            "dry_run": args.dry_run,
            "totals": totals,
            "insufficient_history": insufficient_history,
            "per_ticker": per_ticker,
        }
        json.dump(summary, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
