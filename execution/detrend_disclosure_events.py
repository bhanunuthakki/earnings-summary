"""Cross-sectionally detrend item-level disclosure-change magnitude.

Layer-3 entrypoint for the P2 build (docs/design/disclosure_change_build_stack.md
§P2). Sweeps ``disclosure_events`` + ``filing_sections`` ONCE across the
tracked book to build a same-period cross-section, then percentile-ranks each
ticker's item add/remove/reword volume against its peers for that period
(``filings.cross_sectional_detrend``). Zero LLM — this is arithmetic over
already-persisted counts and lengths.

Must be re-run any time ``execution/detect_disclosure_changes.py`` (P0) has
written new/updated events for the tickers in scope — see
``filings.cross_sectional_detrend``'s module docstring for why the two are
separate steps rather than one fused pass.

Structured events go to stderr, one JSON object per line; a machine-readable
run summary goes to stdout. Exit codes: 0 success, 1 hard stop (missing
migration), 2 bad arguments.

Usage:
    python execution/detrend_disclosure_events.py --all
    python execution/detrend_disclosure_events.py --tickers META,WIX --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from filings import cross_sectional_detrend as csd  # noqa: E402
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--tickers", type=str, default=None, help="Comma-separated tickers to score/write"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Score every ticker present in disclosure_events/filing_sections",
    )
    parser.add_argument("--db-path", type=str, default=None, help="Portfolio DB path override")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute scores but write nothing to the DB",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    log = logging.getLogger("detrend_disclosure_events")

    if not args.all and not args.tickers:
        log.error({"event": "no_scope", "hint": "pass --tickers or --all"})
        return _EXIT_BAD_ARGS

    if args.db_path:
        db.set_db_path(args.db_path)
    conn = db.get_connection()

    try:
        write_scope = (
            None if args.all else [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        )
        if write_scope is not None and not write_scope:
            log.error({"event": "empty_ticker_set"})
            return _EXIT_BAD_ARGS

        started = datetime.now(UTC).replace(tzinfo=None)
        try:
            # Corpus is ALWAYS built over the full tracked book (cross-
            # sectional power needs the widest peer pool); --tickers only
            # scopes which rows get a score computed/written this run.
            corpus = csd.build_cross_sectional_corpus(conn)
        except HardStopError as exc:
            log.error({"event": "hard_stop", "stage": "corpus_build", "error": str(exc)})
            return _EXIT_HARD_STOP

        scores = csd.score_all(corpus, tickers=write_scope)
        gated_off = [s for s in scores if not s.gate_ran]
        scored = [s for s in scores if s.gate_ran]

        written = 0
        if not args.dry_run and scored:
            written = csd.write_detrended_materiality(conn, scored)
            conn.commit()

        per_bucket = [
            {
                "ticker": s.ticker,
                "canonical_id": s.bucket.canonical_id,
                "fiscal_year": s.bucket.fiscal_year,
                "fiscal_period": s.bucket.fiscal_period,
                "raw_item_change_count": s.raw_item_change_count,
                "raw_length_delta_chars": s.raw_length_delta_chars,
                "peer_count": s.peer_count,
                "item_change_percentile": s.item_change_percentile,
                "length_delta_percentile": s.length_delta_percentile,
                "quintile": s.quintile,
                "gate_ran": s.gate_ran,
                "reason": s.reason,
            }
            for s in scores
        ]

        summary: dict[str, object] = {
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "tickers_in_corpus": len(corpus.tickers_covered),
            "buckets_in_corpus": len(corpus.by_bucket),
            "scope": write_scope,
            "dry_run": args.dry_run,
            "scored": len(scored),
            "gated_off_insufficient_peers": len(gated_off),
            "materiality_rows_written": written,
            "per_bucket": per_bucket,
        }
        json.dump(summary, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
