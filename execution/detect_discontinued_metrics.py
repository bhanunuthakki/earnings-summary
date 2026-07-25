"""Detect discontinued/relabeled/standard-transition XBRL metrics — Layer-3
CLI for the P1 build.

Deterministic candidate generation + suppression
(``src/filings/metric_lifecycle.py``: axis-aware gap-calibrated absence
detection, then relabel-pair suppression, then a cross-sectional
accounting-standard-transition gate built once per run over the FULL cached
companyfacts book), then an optional single batched LLM triage call per
ticker (``src/filings/metric_triage.py``), then writes
``metric_discontinued``/``metric_relabeled``/``metric_standard_transition``
rows to ``disclosure_events`` (migration 0203). See
``docs/design/disclosure_change_signals.md`` §2-3.

Every run is idempotent (upsert on ``disclosure_events``'s unique key), so a
partial run resumes simply by running again.

Structured events go to stderr, one JSON object per line; a machine-readable
run summary goes to stdout. Exit codes: 0 success (including recorded
degradations), 1 hard stop (missing migration, LLM budget/setup failure),
2 bad arguments.

Usage:
    python execution/detect_discontinued_metrics.py --tickers META,MELI,VEEV,NU
    python execution/detect_discontinued_metrics.py --all-portfolio --no-llm
    python execution/detect_discontinued_metrics.py --tickers META --dry-run
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
from filings.metric_lifecycle import (  # noqa: E402
    MetricLifecycleEvent,
    StandardTransitionCorpus,
    build_standard_transition_corpus,
    candidate_to_event,
    run_lifecycle_detection,
    write_lifecycle_events,
)
from filings.metric_triage import TriageOutcome, triage_candidates  # noqa: E402
from filings.models import HardStopError  # noqa: E402
from llm.cli import is_hard_stop  # noqa: E402

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


log = logging.getLogger("detect_discontinued_metrics")


def _portfolio_tickers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT ticker FROM tracked_companies WHERE list_type = 'portfolio' "
        "AND COALESCE(instrument_type, '') != 'etf' ORDER BY ticker"
    ).fetchall()
    return [str(r[0]).upper() for r in rows]


def run_ticker(
    *,
    ticker: str,
    repo_root: Path,
    conn: sqlite3.Connection,
    use_llm: bool,
    dry_run: bool,
    standard_transition_corpus: StandardTransitionCorpus | None,
) -> dict[str, object]:
    """Run Stage 0+1+1.5 (always), Stage 2 (unless ``use_llm=False``), and
    write (unless ``dry_run=True``) for one ticker. Never raises on an LLM
    triage failure — that degrades honestly (unclassified verdicts) rather
    than aborting the ticker, per the module's degrade-loud-not-silent
    contract. ``standard_transition_corpus`` should be built ONCE per run
    (see ``main``) and passed to every call, not rebuilt per ticker."""
    result = run_lifecycle_detection(
        repo_root, ticker, standard_transition_corpus=standard_transition_corpus
    )
    if result is None:
        return {"ticker": ticker, "status": "no_companyfacts", "n_events_written": 0}

    triage: TriageOutcome | None = None
    if use_llm and result.candidates:
        try:
            triage = triage_candidates(ticker, result.candidates)
        except Exception as exc:
            if is_hard_stop(exc):
                raise
            log.error(
                {
                    "event": "triage_failed_unclassified",
                    "ticker": ticker,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            triage = None  # degrade to unclassified verdicts below

    events: list[MetricLifecycleEvent] = [
        candidate_to_event(cand, verdict="mechanical") for cand in result.relabeled_pairs
    ] + [
        candidate_to_event(cand, verdict="mechanical") for cand in result.standard_transition_pairs
    ]
    for cand in result.candidates:
        verdict = "unclassified"
        interpretation: str | None = None
        if triage is not None and not triage.degraded:
            v = triage.verdicts.get(cand.qualified_name)
            if v is not None:
                if v.relevance.value == "accounting_plumbing":
                    verdict = "noise"
                elif v.prior.value == "concealment":
                    verdict = "concealment"
                elif v.prior.value == "maturity":
                    verdict = "maturity"
                interpretation = v.rationale
        events.append(candidate_to_event(cand, verdict=verdict, interpretation_md=interpretation))

    n_written = 0
    if not dry_run:
        n_written = write_lifecycle_events(conn, events)
        conn.commit()

    return {
        "ticker": ticker,
        "status": "ok",
        "naive_count": result.naive_count,
        "after_gap_calibration": result.after_gap_calibration,
        "after_relabel_suppression": result.after_relabel_suppression,
        "after_standard_transition_suppression": result.after_standard_transition_suppression,
        "n_discontinued": len(result.candidates),
        "n_relabeled": len(result.relabeled_pairs),
        "n_standard_transition": len(result.standard_transition_pairs),
        "n_events_written": n_written,
        "triage_degraded": bool(triage is not None and triage.degraded),
        "triage_skipped": not use_llm,
        "standard_transition_gate_ran": result.standard_transition_gate_ran,
        "standard_transition_comparison_tickers": result.standard_transition_comparison_tickers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--tickers", type=str, help="Comma-separated tickers")
    g.add_argument("--all-portfolio", action="store_true", help="Every portfolio ticker")
    parser.add_argument("--db-path", type=str, default=None, help="Portfolio DB path override")
    parser.add_argument(
        "--no-llm", action="store_true", help="Deterministic stages only — skip Stage 2 triage"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect (+ triage) but do not write disclosure_events",
    )
    parser.add_argument(
        "--no-cross-sectional",
        action="store_true",
        help=(
            "Skip Stage 1.5's cross-sectional standard-transition gate "
            "(the corpus scan over every cached ticker) — deterministic "
            "stages 0+1 only, no full-book scan. Off by default; every "
            "surviving metric_discontinued candidate is checked."
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    if args.db_path:
        db.set_db_path(args.db_path)
    conn = db.get_connection()
    # Cached companyfacts payloads live alongside the DB, not alongside the
    # code — running from a worktree against a copied DB is the normal case
    # (see docs/design/disclosure_change_signals.md verification notes).
    repo_root = Path(db.DATA_DIR).parent

    try:
        if args.all_portfolio:
            tickers = _portfolio_tickers(conn)
        else:
            tickers = [t.strip().upper() for t in cast("str", args.tickers).split(",") if t.strip()]
        if not tickers:
            log.error({"event": "empty_ticker_set"})
            return _EXIT_BAD_ARGS

        # Stage 1.5's cross-sectional corpus is a FULL scan over every
        # cached ticker (deliberately the whole book, not just the ones
        # requested here or the held portfolio — see
        # list_cached_tickers/build_standard_transition_corpus docstrings
        # for why more comparison points matter). Built ONCE per run and
        # shared across every per-ticker call below, never rebuilt per
        # ticker (that would turn an O(n) run into O(n^2)).
        corpus: StandardTransitionCorpus | None = None
        if not args.no_cross_sectional:
            corpus = build_standard_transition_corpus(repo_root)
            comparison_pool = len(corpus.tickers_covered)
            log.info(
                {
                    "event": "standard_transition_corpus_built",
                    "tickers_covered": comparison_pool,
                    "distinct_tags_with_stop_events": len(corpus.stop_events),
                }
            )
            if comparison_pool <= 1:
                # Absence is never silent: a corpus with <=1 ticker (itself,
                # or nothing at all) means the gate will structurally never
                # fire — every candidate would read as "confirmed
                # company-specific" when really no comparison ever happened.
                log.warning(
                    {
                        "event": "standard_transition_corpus_no_comparison_data",
                        "tickers_covered": comparison_pool,
                        "hint": (
                            "no cached companyfacts found beyond this run's own "
                            "ticker(s) — the cross-sectional gate ran but could "
                            "not compare against anything; every metric_discontinued "
                            "verdict below is UNCONFIRMED against noise source 4"
                        ),
                    }
                )
        else:
            log.warning(
                {
                    "event": "standard_transition_gate_skipped",
                    "hint": "--no-cross-sectional passed; metric_discontinued may include ASU/ASC-driven noise",
                }
            )

        started = datetime.now(UTC).replace(tzinfo=None)
        reports: list[dict[str, object]] = []
        for ticker in tickers:
            try:
                outcome = run_ticker(
                    ticker=ticker,
                    repo_root=repo_root,
                    conn=conn,
                    use_llm=not args.no_llm,
                    dry_run=args.dry_run,
                    standard_transition_corpus=corpus,
                )
            except HardStopError as exc:
                log.error({"event": "hard_stop", "ticker": ticker, "error": str(exc)})
                return _EXIT_HARD_STOP
            log.info({"event": "ticker_complete", **outcome})
            reports.append(outcome)

        summary: dict[str, object] = {
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "tickers": tickers,
            "dry_run": args.dry_run,
            "llm_enabled": not args.no_llm,
            "cross_sectional_gate_ran": corpus is not None,
            "cross_sectional_comparison_tickers": len(corpus.tickers_covered) if corpus else 0,
            "total_events_written": sum(cast("int", r.get("n_events_written", 0)) for r in reports),
            "reports": reports,
        }
        json.dump(summary, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
