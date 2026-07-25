"""Detect transcript longitudinal-tracking events — Layer-3 CLI for the P4
build (``docs/design/disclosure_change_build_stack.md`` P4,
``docs/design/disclosure_change_signals.md`` §1.7).

Deterministic turn-parsing/role-classification/Q&A-pairing
(``src/transcripts/longitudinal.py``), one batched LLM judgment call per
transcript for non-answer classification + per-speaker tone
(``src/transcripts/transcript_judgment.judge_call``, cached — reruns cost
zero new tokens for an already-judged call), one batched LLM triage call
per ticker for KPI/topic relevance (``transcript_judgment.triage_topics``),
then writes ``qa_nonanswer_rate_shift`` / ``transcript_topic_disappeared`` /
``transcript_tone_shift_abnormal`` / ``executive_speaker_change`` /
``analyst_roster_change`` rows to ``disclosure_events`` (migration 0200).

Every run is idempotent (upsert on ``disclosure_events``'s unique key), so a
partial run resumes simply by running again, and a fully-cached ticker
re-run costs zero LLM tokens.

Structured events go to stderr, one JSON object per line; a machine-readable
run summary goes to stdout. Exit codes: 0 success (including recorded
degradations), 1 hard stop (missing migration, LLM budget/setup failure),
2 bad arguments.

Usage:
    python execution/detect_transcript_disclosure_events.py --tickers MELI,NU,NVDA
    python execution/detect_transcript_disclosure_events.py --all-portfolio --no-llm
    python execution/detect_transcript_disclosure_events.py --tickers MELI --dry-run --verbose
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
from llm.cli import is_hard_stop  # noqa: E402
from transcripts.longitudinal import (  # noqa: E402
    ANALYST_ROSTER_MIN_N,
    ANALYST_ROSTER_OVERLAP_FLOOR,
    DETECTOR_VERSION,
    NONANSWER_BASELINE_PCT,
    NONANSWER_MATERIALITY_PP,
    NONANSWER_MIN_QUESTIONS,
    TOPIC_MIN_PRIOR_PRESENCE,
    CallSnapshot,
    SpeakerRole,
    TranscriptEvent,
    build_call_snapshot,
    call_full_text,
    extract_kpi_terms,
    find_kpi_mention,
    jaccard,
    nearest_earnings_surprise,
    roster_names_by_role,
    tone_shift_is_abnormal,
    write_transcript_events,
)
from transcripts.transcript_judgment import CallJudgment, judge_call, triage_topics  # noqa: E402

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


log = logging.getLogger("detect_transcript_disclosure_events")


def _portfolio_tickers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT ticker FROM tracked_companies WHERE list_type = 'portfolio' "
        "AND COALESCE(instrument_type, '') != 'etf' ORDER BY ticker"
    ).fetchall()
    return [str(r[0]).upper() for r in rows]


def _ticker_transcript_ids(conn: sqlite3.Connection, ticker: str) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM transcripts WHERE UPPER(ticker) = ? "
        "AND fiscal_period_type IN ('Q1','Q2','Q3','Q4') AND period_end IS NOT NULL "
        "ORDER BY period_end ASC",
        (ticker.upper(),),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _nonanswer_rate(judgment: CallJudgment) -> tuple[float | None, int]:
    if not judgment.questions:
        return None, 0
    n = len(judgment.questions)
    non_answers = sum(1 for v in judgment.questions.values() if v.non_answer)
    return (non_answers / n) * 100.0, n


def _evidence_for_nonanswer(snap: CallSnapshot, judgment: CallJudgment) -> str:
    """Most confident non-answer example this call, for the receipt."""
    by_index = {ex.index: ex for ex in snap.exchanges}
    for idx_str, verdict in judgment.questions.items():
        if not verdict.non_answer:
            continue
        try:
            idx = int(idx_str)
        except ValueError:
            continue
        ex = by_index.get(idx)
        if ex is None:
            continue
        q = ex.question_text[:300]
        a = ex.answer_text[:300]
        return f'Q ({ex.analyst_name or "unknown"}): "{q}" | A: "{a}" | model rationale: {verdict.rationale}'
    return "(non-answer flagged but exchange text unavailable)"


def run_ticker(
    *,
    ticker: str,
    conn: sqlite3.Connection,
    use_llm: bool,
    dry_run: bool,
    db_path: Path,
) -> dict[str, object]:
    ids = _ticker_transcript_ids(conn, ticker)
    if len(ids) < 2:
        return {
            "ticker": ticker,
            "status": "insufficient_history",
            "n_transcripts": len(ids),
            "n_events_written": 0,
        }

    snapshots: list[CallSnapshot] = []
    for tid in ids:
        snap = build_call_snapshot(conn, tid)
        if snap is not None:
            snapshots.append(snap)

    kpi_terms = extract_kpi_terms(conn, ticker)
    events: list[TranscriptEvent] = []
    coverage_notes: list[dict[str, object]] = []

    # --- Q&A-dependent measures: non-answer rate, tone, exec roster ------
    prev_judgment: CallJudgment | None = None
    prev_snap: CallSnapshot | None = None
    n_calls_with_qa = 0
    nonanswer_rates: list[float] = []
    llm_degraded_calls = 0

    for snap in snapshots:
        fiscal_period = snap.fiscal_period_type
        fiscal_year = snap.fiscal_year
        if not snap.exchanges:
            coverage_notes.append(
                {
                    "transcript_id": snap.transcript_id,
                    "fiscal_period": f"{fiscal_period}-{fiscal_year}",
                    "reason": "no_qa_exchanges_detected",
                    "parse_coverage": snap.parse_coverage,
                }
            )
            continue

        judgment: CallJudgment | None = None
        if use_llm:
            judgment = judge_call(snap, db_path=db_path)
            if judgment.degraded:
                llm_degraded_calls += 1
                judgment = None

        if judgment is not None:
            n_calls_with_qa += 1
            rate, n_judged = _nonanswer_rate(judgment)
            if rate is not None and n_judged >= NONANSWER_MIN_QUESTIONS:
                nonanswer_rates.append(rate)
                vs_baseline = abs(rate - NONANSWER_BASELINE_PCT)
                vs_prior = None
                if prev_judgment is not None:
                    prior_rate, prior_n = _nonanswer_rate(prev_judgment)
                    if prior_rate is not None and prior_n >= NONANSWER_MIN_QUESTIONS:
                        vs_prior = abs(rate - prior_rate)
                material = vs_baseline >= NONANSWER_MATERIALITY_PP or (
                    vs_prior is not None and vs_prior >= NONANSWER_MATERIALITY_PP
                )
                if material:
                    events.append(
                        TranscriptEvent(
                            ticker=ticker,
                            event_type="qa_nonanswer_rate_shift",
                            fiscal_year=fiscal_year,
                            fiscal_period=fiscal_period,
                            source_doc_id=None,
                            subject="qa_nonanswer_rate",
                            subject_label=f"Q&A non-answer rate: {rate:.1f}% (n={n_judged})",
                            current_excerpt=f"{rate:.1f}% of {n_judged} judged questions",
                            evidence_quote=_evidence_for_nonanswer(snap, judgment),
                            materiality=round(vs_baseline, 1),
                            verdict="unclassified",
                            interpretation_md=(
                                f"Deviates {vs_baseline:.1f}pp from the ~{NONANSWER_BASELINE_PCT:.0f}% "
                                "stable baseline (Gow/Larcker/Zakolyukina, JAR 2021)"
                                + (
                                    f"; {vs_prior:.1f}pp vs prior call"
                                    if vs_prior is not None
                                    else ""
                                )
                            ),
                            detector_version=DETECTOR_VERSION,
                        )
                    )

            # Tone, tracked BY NAME (never pooled CEO/CFO) — see
            # transcripts.longitudinal module docstring on exec_role.
            if prev_snap is not None and prev_judgment is not None:
                surprise = nearest_earnings_surprise(conn, ticker, snap.period_end)
                for speaker, verdict in judgment.tone.items():
                    prior_verdict = prev_judgment.tone.get(speaker)
                    if prior_verdict is None:
                        continue  # no comparable prior score for this named person
                    delta = verdict.score - prior_verdict.score
                    eps_pct = surprise.eps_surprise_pct if surprise is not None else None
                    if tone_shift_is_abnormal(delta, eps_pct):
                        exec_role = snap.roster.get(speaker)
                        role_label = (
                            exec_role.exec_role
                            if exec_role and exec_role.exec_role
                            else "unresolved role"
                        )
                        events.append(
                            TranscriptEvent(
                                ticker=ticker,
                                event_type="transcript_tone_shift_abnormal",
                                fiscal_year=fiscal_year,
                                fiscal_period=fiscal_period,
                                prior_fiscal_year=prev_snap.fiscal_year,
                                prior_fiscal_period=prev_snap.fiscal_period_type,
                                subject=speaker,
                                subject_label=f"{speaker} ({role_label}) tone shift",
                                prior_excerpt=f"prior tone {prior_verdict.score:+.2f}: {prior_verdict.rationale}",
                                current_excerpt=f"current tone {verdict.score:+.2f}: {verdict.rationale}",
                                evidence_quote=verdict.rationale,
                                materiality=round(abs(delta), 2),
                                verdict="unclassified",
                                interpretation_md=(
                                    f"Delta {delta:+.2f} on a -1..1 scale; eps_surprise_pct="
                                    f"{eps_pct if eps_pct is not None else 'n/a'}. Proxy for ABTONE "
                                    "residualization (Huang/Teoh/Zhang, TAR 2014) — NOT a fitted "
                                    "OLS residual; see tone_shift_is_abnormal docstring."
                                ),
                                detector_version=DETECTOR_VERSION,
                            )
                        )

        # Executive roster delta (deterministic, no LLM; NOVEL/UNVALIDATED).
        if prev_snap is not None:
            cur_mgmt = roster_names_by_role(snap.roster, SpeakerRole.MANAGEMENT)
            prior_mgmt = roster_names_by_role(prev_snap.roster, SpeakerRole.MANAGEMENT)
            added = cur_mgmt - prior_mgmt
            removed = prior_mgmt - cur_mgmt
            if added or removed:
                sample_name = next(iter(added or removed))
                sample_turn = next(
                    (t.text for t in snap.turns if t.speaker == sample_name),
                    next((t.text for t in prev_snap.turns if t.speaker == sample_name), ""),
                )
                events.append(
                    TranscriptEvent(
                        ticker=ticker,
                        event_type="executive_speaker_change",
                        fiscal_year=fiscal_year,
                        fiscal_period=fiscal_period,
                        prior_fiscal_year=prev_snap.fiscal_year,
                        prior_fiscal_period=prev_snap.fiscal_period_type,
                        subject="executive_roster",
                        subject_label=f"added={sorted(added)} removed={sorted(removed)}",
                        evidence_quote=(
                            sample_turn[:300] or f"{sample_name} (no quotable turn text)"
                        ),
                        materiality=float(len(added) + len(removed)),
                        verdict="unclassified",
                        interpretation_md=(
                            "NOVEL/UNVALIDATED measure (disclosure_change_signals.md §1.7 lists "
                            "executive-speaker change as unvalidated by the literature)."
                        ),
                        detector_version=DETECTOR_VERSION,
                    )
                )

        # Analyst roster delta (deterministic, no LLM; NOVEL/UNVALIDATED).
        if prev_snap is not None:
            cur_analysts = roster_names_by_role(snap.roster, SpeakerRole.ANALYST)
            prior_analysts = roster_names_by_role(prev_snap.roster, SpeakerRole.ANALYST)
            if (
                len(cur_analysts) >= ANALYST_ROSTER_MIN_N
                and len(prior_analysts) >= ANALYST_ROSTER_MIN_N
            ):
                overlap = jaccard(cur_analysts, prior_analysts)
                if overlap < ANALYST_ROSTER_OVERLAP_FLOOR:
                    new_analysts = cur_analysts - prior_analysts
                    sample_name = next(iter(new_analysts), None)
                    sample_turn = (
                        next((t.text for t in snap.turns if t.speaker == sample_name), "")
                        if sample_name
                        else ""
                    )
                    events.append(
                        TranscriptEvent(
                            ticker=ticker,
                            event_type="analyst_roster_change",
                            fiscal_year=fiscal_year,
                            fiscal_period=fiscal_period,
                            prior_fiscal_year=prev_snap.fiscal_year,
                            prior_fiscal_period=prev_snap.fiscal_period_type,
                            subject="analyst_roster",
                            subject_label=(
                                f"overlap={overlap:.0%} cur_n={len(cur_analysts)} "
                                f"prior_n={len(prior_analysts)}"
                            ),
                            evidence_quote=(
                                sample_turn[:300] or "(new analyst; no quotable turn text)"
                            ),
                            materiality=round(1.0 - overlap, 2),
                            verdict="unclassified",
                            interpretation_md=(
                                "NOVEL/UNVALIDATED measure (disclosure_change_signals.md §1.7 lists "
                                "analyst-roster change as unvalidated by the literature)."
                            ),
                            detector_version=DETECTOR_VERSION,
                        )
                    )

        prev_snap = snap
        if judgment is not None:
            prev_judgment = judgment

    # --- KPI/topic presence (deterministic candidates, LLM relevance gate) --
    candidates_by_kpi: dict[str, list[tuple[int, int, str, str]]] = {}
    # kpi_name -> [(fiscal_year, index_in_snapshots, evidence_excerpt, fiscal_period), ...]
    if kpi_terms and len(snapshots) > TOPIC_MIN_PRIOR_PRESENCE:
        full_texts = [call_full_text(s.turns) for s in snapshots]
        for kpi_name, phrases in kpi_terms:
            last_mention: str | None = None
            for i in range(len(snapshots)):
                mention = find_kpi_mention(full_texts[i], phrases)
                if mention is not None:
                    last_mention = mention
                    continue
                if i < TOPIC_MIN_PRIOR_PRESENCE:
                    continue
                prior_slice = full_texts[i - TOPIC_MIN_PRIOR_PRESENCE : i]
                priors_present = all(find_kpi_mention(t, phrases) is not None for t in prior_slice)
                if priors_present and last_mention is not None:
                    candidates_by_kpi.setdefault(kpi_name, []).append(
                        (snapshots[i].fiscal_year, i, last_mention, snapshots[i].fiscal_period_type)
                    )

    if candidates_by_kpi:
        triage_names = sorted(candidates_by_kpi.keys())
        triage_outcome = None
        if use_llm:
            triage_outcome = triage_topics(ticker, triage_names, db_path=db_path)
        for kpi_name, occurrences in candidates_by_kpi.items():
            relevant = True
            rationale = None
            if triage_outcome is not None and not triage_outcome.degraded:
                verdict = triage_outcome.verdicts.get(kpi_name)
                if verdict is not None:
                    relevant = verdict.relevant
                    rationale = verdict.rationale
                else:
                    relevant = False  # unresolved verdict — don't emit unreviewed
            elif use_llm:
                relevant = False  # triage failed/degraded — don't emit unreviewed
            if not relevant:
                continue
            for fiscal_year, _i, last_mention, fiscal_period in occurrences:
                events.append(
                    TranscriptEvent(
                        ticker=ticker,
                        event_type="transcript_topic_disappeared",
                        fiscal_year=fiscal_year,
                        fiscal_period=fiscal_period,
                        subject=kpi_name,
                        subject_label=kpi_name,
                        prior_excerpt=last_mention,
                        evidence_quote=last_mention,
                        materiality=None,
                        verdict="unclassified",
                        interpretation_md=(
                            "NOVEL/UNVALIDATED measure — the literature has no published "
                            "validation for topic disappearance on transcripts specifically "
                            "(nearest analog: guidance withdrawal)."
                            + (f" LLM relevance rationale: {rationale}" if rationale else "")
                        ),
                        detector_version=DETECTOR_VERSION,
                    )
                )

    n_written = 0
    if not dry_run:
        n_written = write_transcript_events(conn, events)
        conn.commit()

    return {
        "ticker": ticker,
        "status": "ok",
        "n_transcripts": len(ids),
        "n_snapshots_built": len(snapshots),
        "n_calls_with_qa_exchanges": n_calls_with_qa,
        "coverage_notes": coverage_notes,
        "nonanswer_rates_measured": [round(r, 1) for r in nonanswer_rates],
        "llm_degraded_calls": llm_degraded_calls,
        "n_kpi_candidates": sum(len(v) for v in candidates_by_kpi.values()),
        "n_events_built": len(events),
        "n_events_written": n_written,
        "llm_enabled": use_llm,
        "dry_run": dry_run,
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
        "--no-llm", action="store_true", help="Deterministic stages only — skip both LLM calls"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Detect (+ judge) but do not write disclosure_events"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    if args.db_path:
        db.set_db_path(args.db_path)
    conn = db.get_connection()
    db_path = Path(args.db_path) if args.db_path else Path(db.DB_PATH)

    try:
        if args.all_portfolio:
            tickers = _portfolio_tickers(conn)
        else:
            tickers = [t.strip().upper() for t in cast("str", args.tickers).split(",") if t.strip()]
        if not tickers:
            log.error({"event": "empty_ticker_set"})
            return _EXIT_BAD_ARGS

        started = datetime.now(UTC).replace(tzinfo=None)
        reports: list[dict[str, object]] = []
        for ticker in tickers:
            try:
                outcome = run_ticker(
                    ticker=ticker,
                    conn=conn,
                    use_llm=not args.no_llm,
                    dry_run=args.dry_run,
                    db_path=db_path,
                )
            except HardStopError as exc:
                log.error({"event": "hard_stop", "ticker": ticker, "error": str(exc)})
                return _EXIT_HARD_STOP
            except Exception as exc:
                if is_hard_stop(exc):
                    log.error({"event": "hard_stop", "ticker": ticker, "error": str(exc)})
                    return _EXIT_HARD_STOP
                raise
            log.info(
                {
                    "event": "ticker_complete",
                    **{k: v for k, v in outcome.items() if k != "coverage_notes"},
                }
            )
            reports.append(outcome)

        summary: dict[str, object] = {
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "tickers": tickers,
            "dry_run": args.dry_run,
            "llm_enabled": not args.no_llm,
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
