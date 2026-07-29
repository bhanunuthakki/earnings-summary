"""Detect transcript longitudinal-tracking events — Layer-3 CLI for the P4
build (``docs/design/disclosure_change_build_stack.md`` P4,
``docs/design/disclosure_change_signals.md`` §1.7, D2.3/D2.4 of
``docs/design/disclosure_intelligence_v1_prd.md``).

Deterministic turn-parsing/role-classification/Q&A-pairing/participants-
roster-parsing (``src/transcripts/longitudinal.py``), one batched LLM
judgment call per transcript for per-speaker tone
(``src/transcripts/transcript_judgment.judge_call``, cached — reruns cost
zero new tokens for an already-judged call), one batched LLM triage call
per ticker for KPI/topic relevance (``transcript_judgment.triage_topics``),
then writes ``abnormal_tone_shift`` / ``transcript_topic_disappeared`` /
``executive_speaker_change`` / ``analyst_roster_change`` rows to
``disclosure_events`` (migration 0203).

DROPPED (D1.6): the "is this answer evasive?" non-answer classification and
its ``qa_nonanswer_rate_shift`` event — owner ruling, the construct never
reproduced Gow/Larcker/Zakolyukina's ~11% baseline against this book. Zero
``qa_nonanswer_rate_shift`` rows were ever written to prod (verified against
a copy on 2026-07-25), so no data purge was needed.

``abnormal_tone_shift`` (D2.3, renamed from ``transcript_tone_shift_
abnormal``) is now emitted from a REAL fitted residual
(``src/transcripts/longitudinal.fit_tone_residual_model`` /
``tone_residual``) — a deterministic pooled OLS of tone score on
fundamentals (eps_surprise_pct), fit ONCE across every requested ticker's
already-cached tone scores before the per-ticker event-emission pass, zero
new LLM calls. Each speaker's ``exec_role`` (CEO/CFO/other_exec/unresolved,
D2.4) is resolved per-call from the transcript's own CORPORATE PARTICIPANTS
roster where one exists, ahead of the (book-wide-empty) ``exec_comp_
packages`` DEF 14A join — see ``longitudinal.parse_participants_roster``.
This module does NOT implement Larcker/Zakolyukina's deception-marker
language model; the tone score is a separate, sentiment-only construct.

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
from provenance.selection import selected_transcripts_relation  # noqa: E402
from transcripts.longitudinal import (  # noqa: E402
    ABTONE_MIN_OBSERVATIONS,
    ABTONE_RESIDUAL_MATERIALITY,
    ANALYST_ROSTER_MIN_N,
    ANALYST_ROSTER_OVERLAP_FLOOR,
    DETECTOR_VERSION,
    TOPIC_MIN_PRIOR_PRESENCE,
    CallSnapshot,
    SpeakerRole,
    ToneObservation,
    ToneResidualModel,
    TranscriptEvent,
    build_call_snapshot,
    call_full_text,
    extract_kpi_terms,
    find_kpi_mention,
    fit_tone_residual_model,
    jaccard,
    nearest_earnings_surprise,
    roster_names_by_role,
    tone_residual,
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
    """One transcript id per (fiscal_period_type, period_end), oldest first.

    Real-book data quality issue found during D2.3/D2.4 verification: some
    tickers (e.g. NVDA) carry TWO ``transcripts`` rows for the identical
    period — one from a segmented source (FactSet/Refinitiv PDF ingest,
    dozens of ``transcript_segments`` rows) and one from the aggregator Q&A
    fetch (a single blob row) — with no dedup at ingest time. Left
    unhandled, every call-over-call measure in this module (tone deltas,
    executive/analyst roster deltas) would compare two representations of
    the SAME real call as if they were sequential quarters. Kept per period:
    the id with the MOST ``transcript_segments`` rows — a proxy for "richer
    source," and not coincidentally the one more likely to carry the
    CORPORATE PARTICIPANTS roster block D2.4 parses. This is a data-hygiene
    symptom in the ingestion path (out of this module's ownership to fix at
    the source); this is a defensive read-side dedup only.
    """
    transcripts_relation = selected_transcripts_relation(conn).sql
    rows = conn.execute(
        f"SELECT id, fiscal_period_type, period_end FROM {transcripts_relation} "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "WHERE UPPER(ticker) = ? "
        "AND fiscal_period_type IN ('Q1','Q2','Q3','Q4') AND period_end IS NOT NULL "
        "ORDER BY period_end ASC",
        (ticker.upper(),),
    ).fetchall()
    best_by_period: dict[tuple[str, str], tuple[int, int]] = {}
    order: list[tuple[str, str]] = []
    for id_raw, fpt_raw, period_end_raw in rows:
        key = (str(fpt_raw), str(period_end_raw))
        n_segments = conn.execute(
            "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id = ?", (int(id_raw),)
        ).fetchone()[0]
        if key not in best_by_period:
            order.append(key)
        if key not in best_by_period or n_segments > best_by_period[key][1]:
            best_by_period[key] = (int(id_raw), int(n_segments))
    return [best_by_period[key][0] for key in order]


def _build_snapshots_and_judgments(
    *,
    ticker: str,
    conn: sqlite3.Connection,
    use_llm: bool,
    db_path: Path,
) -> tuple[list[CallSnapshot], dict[int, CallJudgment], int]:
    """Phase 1: deterministic snapshots + (cached-first) tone judgments for
    one ticker. Returns (snapshots, judgments_by_transcript_id,
    llm_degraded_calls). Split out from event emission so ``main()`` can
    pool every requested ticker's tone observations and fit ONE
    ``ToneResidualModel`` (D2.3) before any ticker's events are built."""
    ids = _ticker_transcript_ids(conn, ticker)
    snapshots: list[CallSnapshot] = []
    for tid in ids:
        snap = build_call_snapshot(conn, tid)
        if snap is not None:
            snapshots.append(snap)

    judgments: dict[int, CallJudgment] = {}
    llm_degraded_calls = 0
    for snap in snapshots:
        if not snap.exchanges:
            continue
        if not use_llm:
            continue
        judgment = judge_call(snap, db_path=db_path)
        if judgment.degraded:
            llm_degraded_calls += 1
        else:
            judgments[snap.transcript_id] = judgment
    return snapshots, judgments, llm_degraded_calls


def collect_tone_observations(
    *,
    ticker: str,
    conn: sqlite3.Connection,
    snapshots: list[CallSnapshot],
    judgments: dict[int, CallJudgment],
) -> list[ToneObservation]:
    """Flatten one ticker's cached tone judgments into
    ``ToneObservation`` rows for the pooled D2.3 residual fit. Zero new LLM
    calls — reads only what ``judgments`` already holds."""
    observations: list[ToneObservation] = []
    for snap in snapshots:
        judgment = judgments.get(snap.transcript_id)
        if judgment is None:
            continue
        surprise = nearest_earnings_surprise(conn, ticker, snap.period_end)
        for speaker, verdict in judgment.tone.items():
            observations.append(
                ToneObservation(
                    ticker=ticker,
                    fiscal_period_type=snap.fiscal_period_type,
                    fiscal_year=snap.fiscal_year,
                    period_end=snap.period_end,
                    speaker=speaker,
                    tone_score=verdict.score,
                    eps_surprise_pct=surprise.eps_surprise_pct if surprise else None,
                    revenue_surprise_pct=surprise.revenue_surprise_pct if surprise else None,
                )
            )
    return observations


def run_ticker(
    *,
    ticker: str,
    conn: sqlite3.Connection,
    snapshots: list[CallSnapshot],
    judgments: dict[int, CallJudgment],
    llm_degraded_calls: int,
    residual_model: ToneResidualModel | None,
    use_llm: bool,
    dry_run: bool,
    db_path: Path,
) -> dict[str, object]:
    """Phase 2: build + write this ticker's events from ALREADY-built
    snapshots/judgments (phase 1) and the pooled residual model (fit once,
    across every requested ticker, by ``main()``)."""
    if len(snapshots) < 2:
        return {
            "ticker": ticker,
            "status": "insufficient_history",
            "n_transcripts": len(snapshots),
            "n_events_written": 0,
        }

    kpi_terms = extract_kpi_terms(conn, ticker)
    events: list[TranscriptEvent] = []
    coverage_notes: list[dict[str, object]] = []

    # --- Q&A-dependent measures: tone residual, exec/analyst roster -----
    prev_snap: CallSnapshot | None = None
    n_calls_with_qa = 0

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

        judgment = judgments.get(snap.transcript_id)
        prev_judgment = judgments.get(prev_snap.transcript_id) if prev_snap is not None else None

        if judgment is not None:
            n_calls_with_qa += 1
            # ABTONE (D2.3) — the REAL fitted residual, not the retired
            # tone_delta_heuristic_flag proxy. Tracked BY NAME (never pooled
            # CEO/CFO) — see transcripts.longitudinal module docstring on
            # exec_role (D2.4 resolves it per-call from the transcript's own
            # CORPORATE PARTICIPANTS roster where one exists).
            if prev_snap is not None and prev_judgment is not None and residual_model is not None:
                surprise_cur = nearest_earnings_surprise(conn, ticker, snap.period_end)
                surprise_prev = nearest_earnings_surprise(conn, ticker, prev_snap.period_end)
                for speaker, verdict in judgment.tone.items():
                    prior_verdict = prev_judgment.tone.get(speaker)
                    if prior_verdict is None:
                        continue  # no comparable prior score for this named person
                    obs_cur = ToneObservation(
                        ticker=ticker,
                        fiscal_period_type=fiscal_period,
                        fiscal_year=fiscal_year,
                        period_end=snap.period_end,
                        speaker=speaker,
                        tone_score=verdict.score,
                        eps_surprise_pct=surprise_cur.eps_surprise_pct if surprise_cur else None,
                        revenue_surprise_pct=(
                            surprise_cur.revenue_surprise_pct if surprise_cur else None
                        ),
                    )
                    obs_prev = ToneObservation(
                        ticker=ticker,
                        fiscal_period_type=prev_snap.fiscal_period_type,
                        fiscal_year=prev_snap.fiscal_year,
                        period_end=prev_snap.period_end,
                        speaker=speaker,
                        tone_score=prior_verdict.score,
                        eps_surprise_pct=surprise_prev.eps_surprise_pct if surprise_prev else None,
                        revenue_surprise_pct=(
                            surprise_prev.revenue_surprise_pct if surprise_prev else None
                        ),
                    )
                    resid_cur = tone_residual(residual_model, obs_cur)
                    resid_prev = tone_residual(residual_model, obs_prev)
                    if resid_cur is None or resid_prev is None:
                        continue  # missing control this call — honest skip, no fabricated 0
                    delta_resid = resid_cur - resid_prev
                    if abs(delta_resid) >= ABTONE_RESIDUAL_MATERIALITY:
                        exec_role = snap.roster.get(speaker)
                        role_label = (
                            exec_role.exec_role
                            if exec_role and exec_role.exec_role
                            else "unresolved role"
                        )
                        events.append(
                            TranscriptEvent(
                                ticker=ticker,
                                event_type="abnormal_tone_shift",
                                fiscal_year=fiscal_year,
                                fiscal_period=fiscal_period,
                                prior_fiscal_year=prev_snap.fiscal_year,
                                prior_fiscal_period=prev_snap.fiscal_period_type,
                                subject=speaker,
                                subject_label=f"{speaker} ({role_label}) abnormal tone shift",
                                prior_excerpt=(
                                    f"prior residual {resid_prev:+.2f} "
                                    f"(raw tone {prior_verdict.score:+.2f}): {prior_verdict.rationale}"
                                ),
                                current_excerpt=(
                                    f"current residual {resid_cur:+.2f} "
                                    f"(raw tone {verdict.score:+.2f}): {verdict.rationale}"
                                ),
                                evidence_quote=verdict.rationale,
                                materiality=round(abs(delta_resid), 2),
                                verdict="unclassified",
                                interpretation_md=(
                                    f"Residual delta {delta_resid:+.2f} on a -1..1 scale — the real "
                                    "ABTONE construct (Huang/Teoh/Zhang, TAR 2014): tone residualized "
                                    f"against eps_surprise_pct (fitted book-wide, R^2={residual_model.r_squared:.3f}, "
                                    f"n={residual_model.n_obs}). A within-book deviation flag, not a "
                                    "claim of statistical significance — see fit_tone_residual_model "
                                    "docstring. NOT the retired tone_delta_heuristic_flag proxy."
                                ),
                                detector_version=DETECTOR_VERSION,
                            )
                        )
            elif prev_snap is not None and residual_model is None:
                coverage_notes.append(
                    {
                        "transcript_id": snap.transcript_id,
                        "fiscal_period": f"{fiscal_period}-{fiscal_year}",
                        "reason": "no_residual_model_insufficient_pooled_observations",
                    }
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
        "n_transcripts": len(snapshots),
        "n_snapshots_built": len(snapshots),
        "n_calls_with_qa_exchanges": n_calls_with_qa,
        "coverage_notes": coverage_notes,
        "llm_degraded_calls": llm_degraded_calls,
        "residual_model_used": residual_model is not None,
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
        use_llm = not args.no_llm

        # Phase 1: build snapshots + (cached-first) tone judgments for EVERY
        # requested ticker before fitting anything — the D2.3 residual model
        # is pooled across the whole requested set, not per-ticker, since no
        # single ticker has enough quarters on file for a within-ticker fit
        # to have any power (see fit_tone_residual_model docstring).
        snapshots_by_ticker: dict[str, list[CallSnapshot]] = {}
        judgments_by_ticker: dict[str, dict[int, CallJudgment]] = {}
        degraded_by_ticker: dict[str, int] = {}
        for ticker in tickers:
            try:
                snaps, judgs, degraded = _build_snapshots_and_judgments(
                    ticker=ticker, conn=conn, use_llm=use_llm, db_path=db_path
                )
            except HardStopError as exc:
                log.error({"event": "hard_stop", "ticker": ticker, "error": str(exc)})
                return _EXIT_HARD_STOP
            except Exception as exc:
                if is_hard_stop(exc):
                    log.error({"event": "hard_stop", "ticker": ticker, "error": str(exc)})
                    return _EXIT_HARD_STOP
                raise
            snapshots_by_ticker[ticker] = snaps
            judgments_by_ticker[ticker] = judgs
            degraded_by_ticker[ticker] = degraded

        observations: list[ToneObservation] = []
        for ticker in tickers:
            observations.extend(
                collect_tone_observations(
                    ticker=ticker,
                    conn=conn,
                    snapshots=snapshots_by_ticker[ticker],
                    judgments=judgments_by_ticker[ticker],
                )
            )
        residual_model = fit_tone_residual_model(observations)
        log.info(
            {
                "event": "tone_residual_model_fit",
                "n_observations_pooled": len(observations),
                "n_obs_used": residual_model.n_obs if residual_model else 0,
                "r_squared": residual_model.r_squared if residual_model else None,
                "fitted": residual_model is not None,
                "min_required": ABTONE_MIN_OBSERVATIONS,
            }
        )

        # Phase 2: emit + write events per ticker using the shared model.
        reports: list[dict[str, object]] = []
        for ticker in tickers:
            try:
                outcome = run_ticker(
                    ticker=ticker,
                    conn=conn,
                    snapshots=snapshots_by_ticker[ticker],
                    judgments=judgments_by_ticker[ticker],
                    llm_degraded_calls=degraded_by_ticker[ticker],
                    residual_model=residual_model,
                    use_llm=use_llm,
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
            "llm_enabled": use_llm,
            "residual_model_n_obs": residual_model.n_obs if residual_model else 0,
            "residual_model_r_squared": residual_model.r_squared if residual_model else None,
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
