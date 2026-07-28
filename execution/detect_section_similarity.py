"""Detect detrended document-level YoY similarity shifts — D2.2 Layer-3 CLI
(the Lazy Prices construct: Cohen, Malloy & Nguyen, *JF* 2020).

Aggregates ``filings.item_diff``'s item-grain Jaccard similarity to whole-
canonical-section grain (``filings.section_similarity``), then detrends each
ticker's raw change magnitude against BOTH (a) the same-period whole tracked
book and (b) the ticker's frozen comparable set
(``compute.comparable_sets`` / ``execution/build_comparable_sets.py`` — run
that first; this script never resolves comp sets itself). Book-level
detrending is MANDATORY: a score with no book-level percentile (too few
peers this run) is never persisted, because an un-detrended similarity
measure manufactures a spurious "everyone changes more every year" trend
(Dyer, Lang & Stice-Lawrence, *JAE* 2017). Peer-group detrending degrades
honestly to absent when a ticker has no frozen comparable set.

Writes ``section_similarity_shift`` rows to ``disclosure_events`` (migration
0203): ``materiality`` = whole-book percentile, ``confidence`` = peer-group
percentile (a deliberate reuse of that column for a NON-LLM detector — see
``filings.section_similarity`` module docstring for why). Zero LLM calls —
fully deterministic.

Structured events go to stderr, one JSON object per line; a machine-readable
run summary goes to stdout. Exit codes: 0 success, 1 hard stop (missing
migration), 2 bad arguments.

Usage:
    python execution/detect_section_similarity.py --tickers NU,MELI,VEEV
    python execution/detect_section_similarity.py --all-tracked --dry-run
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
from filings.models import HardStopError  # noqa: E402
from filings.section_similarity import (  # noqa: E402
    SimilarityEvent,
    build_similarity_corpus,
    score_all,
    score_to_event,
    write_similarity_events,
)
from provenance.selection import selected_filing_sections_relation  # noqa: E402

_DEFAULT_CANONICAL_IDS = ("risk_factors", "mdna")
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


log = logging.getLogger("detect_section_similarity")


def _tracked_tickers(conn: sqlite3.Connection) -> list[str]:
    relation = selected_filing_sections_relation(conn).sql
    rows = conn.execute(f"SELECT DISTINCT ticker FROM {relation} ORDER BY ticker").fetchall()
    return [str(r[0]).upper() for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--tickers", type=str, help="Comma-separated tickers to SCORE and write")
    g.add_argument(
        "--all-tracked", action="store_true", help="Every ticker with stored filing_sections"
    )
    parser.add_argument(
        "--canonical-ids",
        type=str,
        default=None,
        help=f"Comma-separated canonical_id concepts (default: {','.join(_DEFAULT_CANONICAL_IDS)})",
    )
    parser.add_argument("--db-path", type=str, default=None, help="Portfolio DB path override")
    parser.add_argument(
        "--dry-run", action="store_true", help="Compute scores but write nothing to the DB"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    if args.db_path:
        db.set_db_path(args.db_path)
    conn = db.get_connection()

    try:
        try:
            conn.execute("SELECT 1 FROM disclosure_events LIMIT 1").fetchone()
            relation = selected_filing_sections_relation(conn).sql
            conn.execute(f"SELECT 1 FROM {relation} LIMIT 1").fetchone()
        except sqlite3.OperationalError as exc:
            log.error({"event": "hard_stop", "stage": "preflight", "error": str(exc)})
            return _EXIT_HARD_STOP

        canonical_ids = (
            tuple(t.strip() for t in args.canonical_ids.split(",") if t.strip())
            if args.canonical_ids
            else _DEFAULT_CANONICAL_IDS
        )

        if args.all_tracked:
            score_tickers = _tracked_tickers(conn)
        else:
            score_tickers = [
                t.strip().upper() for t in cast("str", args.tickers).split(",") if t.strip()
            ]
        if not score_tickers:
            log.error({"event": "empty_ticker_set"})
            return _EXIT_BAD_ARGS

        started = datetime.now(UTC).replace(tzinfo=None)
        # The book-level corpus is a FULL scan over every tracked ticker,
        # regardless of which tickers this run scores — same reasoning as
        # every other Stage-1.5-shaped gate in this program: cross-sectional
        # statistical power comes from MORE comparison points, built once
        # per run, never rebuilt per ticker.
        corpus = build_similarity_corpus(conn, canonical_ids=canonical_ids)
        log.info(
            {
                "event": "similarity_corpus_built",
                "tickers_covered": len(corpus.tickers_covered),
                "buckets": len(corpus.by_bucket),
            }
        )

        scores = score_all(conn, corpus, tickers=score_tickers)
        events: list[SimilarityEvent] = []
        skipped_undetrended = 0
        for s in scores:
            ev = score_to_event(s)
            if ev is None:
                skipped_undetrended += 1
                continue
            events.append(ev)

        n_written = 0
        if not args.dry_run and events:
            n_written = write_similarity_events(conn, events)
            conn.commit()

        per_ticker: dict[str, dict[str, object]] = {}
        for ev in events:
            d = per_ticker.setdefault(
                ev.ticker, {"n_events": 0, "canonical_ids": set(), "with_peer_pct": 0}
            )
            d["n_events"] = cast("int", d["n_events"]) + 1
            cast("set[str]", d["canonical_ids"]).add(ev.canonical_id)
            if ev.peer_percentile is not None:
                d["with_peer_pct"] = cast("int", d["with_peer_pct"]) + 1
        per_ticker_report = [
            {
                "ticker": t,
                "n_events": v["n_events"],
                "canonical_ids": sorted(cast("set[str]", v["canonical_ids"])),
                "with_peer_percentile": v["with_peer_pct"],
            }
            for t, v in sorted(per_ticker.items())
        ]

        summary: dict[str, object] = {
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "canonical_ids": list(canonical_ids),
            "score_tickers": score_tickers,
            "dry_run": args.dry_run,
            "corpus_tickers_covered": len(corpus.tickers_covered),
            "n_scores_computed": len(scores),
            "n_events_written": n_written,
            "n_skipped_undetrended": skipped_undetrended,
            "per_ticker": per_ticker_report,
        }
        json.dump(summary, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0
    except HardStopError as exc:
        log.error({"event": "hard_stop", "error": str(exc)})
        return _EXIT_HARD_STOP
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
