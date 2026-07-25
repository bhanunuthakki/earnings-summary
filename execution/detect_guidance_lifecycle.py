"""Detect guidance-withdrawal (and resumption) events — D2.1 Layer-3 CLI.

Generalizes P1's ``filings.metric_lifecycle`` own-cadence engine to a second
subject family (docs/design/disclosure_intelligence_v1_prd.md D2.1;
docs/design/disclosure_gap_scoping.md Gap 1): Lane A tracks a ticker's whole
``management_commitments`` guidance PRACTICE against its own coverage-known
cadence; Lane B tracks each MD&A "Outlook"/"Guidance" heading
(``filing_section_items``) against its own presence cadence. Both lanes run
Stage 0 (gap calibration) + Stage "1.5" (cross-sectional wave suppression,
built once per run over every ticker with the relevant substrate — never
per-ticker, which would turn an O(n) run into O(n^2)), then an optional
single batched LLM triage call per ticker
(``filings.guidance_triage.triage_guidance_candidates``), then writes
``guidance_withdrawn``/``guidance_resumed`` rows to ``disclosure_events``
(migration 0203).

Every run is idempotent (upsert on ``disclosure_events``'s unique key), so a
partial run resumes simply by running again.

Structured events go to stderr, one JSON object per line; a machine-readable
run summary goes to stdout. Exit codes: 0 success (including recorded
degradations), 1 hard stop (missing migration, LLM budget/setup failure),
2 bad arguments.

Usage:
    python execution/detect_guidance_lifecycle.py --tickers NU,MELI,VEEV
    python execution/detect_guidance_lifecycle.py --all-portfolio --no-llm
    python execution/detect_guidance_lifecycle.py --tickers NU --dry-run
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
from filings.guidance_lifecycle import (  # noqa: E402
    GUIDANCE_WAVE_KEY,
    MDNA_WAVE_KEY,
    GuidanceCandidate,
    GuidanceLifecycleEvent,
    apply_wave_suppression,
    build_commitment_wave_corpus,
    build_mdna_wave_corpus,
    candidate_to_event,
    detect_commitment_lifecycle,
    detect_mdna_lifecycle,
    load_commitment_periods,
    write_guidance_events,
)
from filings.guidance_triage import GuidanceTriageOutcome, triage_guidance_candidates  # noqa: E402
from filings.metric_lifecycle import StandardTransitionCorpus  # noqa: E402
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


log = logging.getLogger("detect_guidance_lifecycle")


def _portfolio_tickers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT ticker FROM tracked_companies WHERE list_type = 'portfolio' "
        "AND COALESCE(instrument_type, '') != 'etf' ORDER BY ticker"
    ).fetchall()
    return [str(r[0]).upper() for r in rows]


def run_ticker(
    *,
    ticker: str,
    conn: sqlite3.Connection,
    use_llm: bool,
    dry_run: bool,
    commitment_wave_corpus: StandardTransitionCorpus | None,
    mdna_wave_corpus: StandardTransitionCorpus | None,
) -> dict[str, object]:
    """Run both lanes' Stage 0 + Stage "1.5" (always), Stage 2/3 (unless
    ``use_llm=False``), and write (unless ``dry_run=True``) for one ticker."""
    periods = load_commitment_periods(conn, ticker)
    commitment_result = detect_commitment_lifecycle(ticker, periods)
    mdna_candidates = detect_mdna_lifecycle(conn, ticker)

    candidates: list[GuidanceCandidate] = []
    wave_flags: dict[str, bool] = {}
    for cand in (commitment_result.withdrawn, commitment_result.resumed):
        if cand is None:
            continue
        cand, is_wave = apply_wave_suppression(
            cand, commitment_wave_corpus, wave_key=GUIDANCE_WAVE_KEY
        )
        candidates.append(cand)
        wave_flags[cand.subject_key + ":" + cand.kind] = is_wave
    for cand in mdna_candidates:
        cand, is_wave = apply_wave_suppression(cand, mdna_wave_corpus, wave_key=MDNA_WAVE_KEY)
        candidates.append(cand)
        wave_flags[cand.subject_key + ":" + cand.kind] = is_wave

    triage: GuidanceTriageOutcome | None = None
    if use_llm and candidates:
        try:
            triage = triage_guidance_candidates(ticker, candidates)
        except Exception as exc:
            if is_hard_stop(exc):
                raise
            log.error(
                {
                    "event": "guidance_triage_failed_unclassified",
                    "ticker": ticker,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            triage = None

    events: list[GuidanceLifecycleEvent] = []
    for cand in candidates:
        is_wave = wave_flags.get(cand.subject_key + ":" + cand.kind, False)
        verdict = "unclassified"
        interpretation: str | None = None
        if not is_wave and triage is not None and not triage.degraded:
            v = triage.verdicts.get(cand.subject_key)
            if v is not None:
                if v.relevance.value == "not_guidance":
                    verdict = "noise"
                elif v.prior.value == "concealment":
                    verdict = "concealment"
                elif v.prior.value == "maturity":
                    verdict = "maturity"
                interpretation = v.rationale
        events.append(
            candidate_to_event(
                cand, is_mechanical_wave=is_wave, verdict=verdict, interpretation_md=interpretation
            )
        )

    n_written = 0
    if not dry_run and events:
        n_written = write_guidance_events(conn, events)
        conn.commit()

    return {
        "ticker": ticker,
        "status": "ok",
        "commitments_coverage_known_periods": commitment_result.n_known_periods,
        "commitments_present_periods": commitment_result.n_present_periods,
        "commitments_insufficient_history": commitment_result.insufficient_history,
        "n_mdna_headings_tracked": len({c.subject_key for c in mdna_candidates}),
        "n_candidates": len(candidates),
        "n_withdrawn": sum(1 for c in candidates if c.kind == "guidance_withdrawn"),
        "n_resumed": sum(1 for c in candidates if c.kind == "guidance_resumed"),
        "n_wave_suppressed": sum(1 for v in wave_flags.values() if v),
        "n_events_written": n_written,
        "triage_degraded": bool(triage is not None and triage.degraded),
        "triage_skipped": not use_llm or not candidates,
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
        "--no-llm", action="store_true", help="Deterministic stages only — skip Stage 2/3 triage"
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
            "Skip the Stage 1.5 wave-suppression gates (the corpus scans over "
            "every ticker with commitment/mdna substrate) — deterministic "
            "Stage 0 only. Off by default; every surviving withdrawal is checked."
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    if args.db_path:
        db.set_db_path(args.db_path)
    conn = db.get_connection()

    try:
        if args.all_portfolio:
            tickers = _portfolio_tickers(conn)
        else:
            tickers = [t.strip().upper() for t in cast("str", args.tickers).split(",") if t.strip()]
        if not tickers:
            log.error({"event": "empty_ticker_set"})
            return _EXIT_BAD_ARGS

        try:
            conn.execute("SELECT 1 FROM disclosure_events LIMIT 1").fetchone()
        except sqlite3.OperationalError as exc:
            log.error({"event": "hard_stop", "stage": "preflight", "error": str(exc)})
            return _EXIT_HARD_STOP

        commitment_corpus: StandardTransitionCorpus | None = None
        mdna_corpus: StandardTransitionCorpus | None = None
        if not args.no_cross_sectional:
            commitment_corpus = build_commitment_wave_corpus(conn)
            # Full-book scope by default (mirrors build_commitment_wave_corpus
            # and metric_lifecycle.build_standard_transition_corpus) -- NOT
            # narrowed to this run's requested tickers, so the wave gate has
            # real cross-sectional power regardless of how few tickers this
            # particular invocation is scoring.
            mdna_corpus = build_mdna_wave_corpus(conn)
            log.info(
                {
                    "event": "wave_corpora_built",
                    "commitments_tickers_covered": len(commitment_corpus.tickers_covered),
                    "mdna_tickers_covered": len(mdna_corpus.tickers_covered),
                }
            )
        else:
            log.warning(
                {
                    "event": "wave_gate_skipped",
                    "hint": "--no-cross-sectional passed; withdrawn candidates may include a macro-wide suspension wave (e.g. COVID-era guidance suspensions)",
                }
            )

        started = datetime.now(UTC).replace(tzinfo=None)
        reports: list[dict[str, object]] = []
        for ticker in tickers:
            try:
                outcome = run_ticker(
                    ticker=ticker,
                    conn=conn,
                    use_llm=not args.no_llm,
                    dry_run=args.dry_run,
                    commitment_wave_corpus=commitment_corpus,
                    mdna_wave_corpus=mdna_corpus,
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
            "cross_sectional_gate_ran": commitment_corpus is not None,
            "total_events_written": sum(cast("int", r.get("n_events_written", 0)) for r in reports),
            "total_withdrawn": sum(cast("int", r.get("n_withdrawn", 0)) for r in reports),
            "total_resumed": sum(cast("int", r.get("n_resumed", 0)) for r in reports),
            "reports": reports,
        }
        json.dump(summary, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
