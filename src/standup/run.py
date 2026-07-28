"""Orchestrate one proactive-standup pass — watch, compose, gate, deliver.

The per-signal flow (ranked by materiality, highest first), with every cheap
gate BEFORE the paid LLM steps so the run spends only on trips that survive
dedup, the per-name cooldown, and the per-day cap:

    for each signal:
        per-day cap reached?         -> stop (rest are day-capped)
        composed within dedup window? -> skip (no re-pay)
        ticker in cooldown?          -> skip
        compose via the Ask engine    (LLM)
          no narrative answer?       -> record compose_failed, continue
        eval-gate the brief           (LLM judge)
          judge call/parse FAILED?   -> record judge_failed, RETRY next run
                                        (infra miss, not a quality verdict —
                                        standup.gate.GateOutcome.judge_failed)
          genuinely scored, < caveat_floor?      -> record suppressed_eval, continue
          genuinely scored, [caveat_floor, min_score)? -> deliver WITH a one-line
                                        caveat prefix (delivered_caveat)
        deliver: append [user headline] + [assistant brief] to the rolling
                 standup thread, distill the conclusion (memory stub),
                 record delivered (or delivered_caveat).

The composed thread lives in ONE persistent ``ask_sessions`` row per user (title
"Analyst standup") so the owner sees a single waiting advisory thread that
accumulates over time. Composition runs the engine WITHOUT that thread's history
(a fresh ``AskTurn``) so each brief is grounded in the packs, not in unrelated
prior trips; continuity is carried deliberately through the per-ticker memory
stub instead.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import standup.ledger as ledger
from ask import store as ask_store
from evals.rubric_judge import LlmCaller, Rubric
from identity import DEFAULT_USER_ID
from llm.cli import call_llm
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from standup.compose import ComposedMessage, EventsFn, compose
from standup.config import StandupConfig
from standup.gate import GateOutcome, gate_message
from standup.memory import distill_conclusion, recall_conclusion
from standup.signals import StandupSignal, collect_signals

log = logging.getLogger(__name__)

STANDUP_SESSION_TITLE = "Analyst standup"
STANDUP_SESSION_SCOPE = "portfolio"
_STANDUP_MODEL_TAG = "standup/ask_answer"

# The deliver-with-caveat prefix (navigation_ia.md §3.3). Prepended to the
# THREAD text only — the ledger's `conclusion` column distills the raw
# `composed.answer` (unprefixed), so the memory stub never carries the
# caveat wording into a future brief's framing.
_CAVEAT_PREFIX = "(below the usual bar - delivered so the channel stays alive)\n\n"


@dataclass(frozen=True, slots=True)
class DeliveredBrief:
    """One brief that cleared the gate and landed in the thread."""

    kind: str
    ticker: str | None
    headline: str
    # None never reaches a delivered brief in practice (a judge-infra outcome
    # is retried, not delivered) — Optional only because GateOutcome.score is.
    score: float | None
    session_id: str
    turn_id: int | None
    answer: str


@dataclass(slots=True)
class StandupReport:
    """Tally of one run — every signal accounted for, plus the delivered briefs."""

    signals_found: int = 0
    deduped: int = 0
    cooldown_skipped: int = 0
    day_capped: int = 0
    composed: int = 0
    delivered: int = 0
    delivered_caveat: int = 0
    suppressed_eval: int = 0
    judge_failed: int = 0
    compose_failed: int = 0
    briefs: list[DeliveredBrief] = field(default_factory=list[DeliveredBrief])

    def as_dict(self) -> dict[str, object]:
        return {
            "signals_found": self.signals_found,
            "deduped": self.deduped,
            "cooldown_skipped": self.cooldown_skipped,
            "day_capped": self.day_capped,
            "composed": self.composed,
            "delivered": self.delivered,
            "delivered_caveat": self.delivered_caveat,
            "suppressed_eval": self.suppressed_eval,
            "judge_failed": self.judge_failed,
            "compose_failed": self.compose_failed,
        }


def _ensure_standup_session(db_path: Path, *, scope: str = STANDUP_SESSION_SCOPE) -> str:
    """The single rolling standup thread for the owner — reused across runs by
    its title, created on first delivery."""
    for sess in ask_store.list_sessions(scope=scope, db_path=db_path):
        if sess.title == STANDUP_SESSION_TITLE:
            return sess.id
    return ask_store.create_session(scope=scope, title=STANDUP_SESSION_TITLE, db_path=db_path).id


def _latest_assistant_turn_id(conn: sqlite3.Connection, session_id: str) -> int | None:
    try:
        row = conn.execute(
            "SELECT MAX(id) FROM ask_turns WHERE session_id = ? AND role = 'assistant'",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and row[0] is not None else None


def _deliver(
    conn: sqlite3.Connection,
    db_path: Path,
    signal: StandupSignal,
    answer: str,
    citations: list[dict[str, object]],
    *,
    session_id: str,
) -> int | None:
    """Append the [user headline] + [assistant brief] turns to the thread and
    return the assistant turn's id."""
    ask_store.append_turn(session_id=session_id, role="user", text=signal.headline, db_path=db_path)
    ask_store.append_turn(
        session_id=session_id,
        role="assistant",
        text=answer,
        citations=citations or None,
        model=_STANDUP_MODEL_TAG,
        db_path=db_path,
    )
    return _latest_assistant_turn_id(conn, session_id)


def run_standup(
    *,
    db_path: Path,
    repo_root: Path,
    events_fn: EventsFn,
    rubric: Rubric,
    now: datetime,
    user_id: str = DEFAULT_USER_ID,
    judge_caller: LlmCaller | None = None,
    config: StandupConfig | None = None,
    dry_run: bool = False,
) -> StandupReport:
    """Run one standup pass. ``events_fn`` (compose) and ``judge_caller`` (gate)
    are the two LLM seams — tests inject fakes; production wires the engine +
    ``call_llm``. ``dry_run`` composes + gates but neither persists the thread
    nor writes the ledger (a preview)."""
    cfg = config or StandupConfig()
    caller: LlmCaller = judge_caller if judge_caller is not None else call_llm

    report = StandupReport()
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        signals = collect_signals(conn, user_id=user_id, repo_root=repo_root, now=now, config=cfg)
        report.signals_found = len(signals)
        if not signals:
            return report

        dedup = ledger.recently_composed_signatures(
            conn, user_id=user_id, now=now, within_days=cfg.dedup_days
        )
        delivered_today = ledger.delivered_today_count(conn, user_id=user_id, now=now)
        session_id: str | None = None

        for signal in signals:
            # A caveat delivery still occupies a thread slot (delivered_today_count
            # counts both statuses — see standup.ledger._DELIVERED_STATUSES).
            if delivered_today + report.delivered + report.delivered_caveat >= cfg.max_per_day:
                report.day_capped += 1
                continue
            if ledger.signature_sha(signal) in dedup:
                report.deduped += 1
                continue
            if ledger.within_cooldown(
                conn,
                user_id=user_id,
                ticker=signal.ticker,
                now=now,
                cooldown_days=cfg.per_name_cooldown_days,
            ):
                report.cooldown_skipped += 1
                continue

            prior = recall_conclusion(conn, user_id=user_id, ticker=signal.ticker)
            composed = compose(signal, events_fn=events_fn, prior=prior)
            if composed is None:
                report.compose_failed += 1
                if not dry_run:
                    ledger.record(
                        conn,
                        user_id=user_id,
                        signal=signal,
                        status=ledger.STATUS_COMPOSE_FAILED,
                        now=now,
                    )
                continue
            report.composed += 1

            outcome = gate_message(
                signal, composed, rubric=rubric, min_score=cfg.min_score, caller=caller
            )

            def _record_and_deliver(
                sig: StandupSignal,
                composed: ComposedMessage,
                outcome: GateOutcome,
                *,
                caveat: bool,
            ) -> None:
                """Append the thread turns + ledger row for a clean or
                caveat-tier delivery. ``sig``/``composed``/``outcome`` are
                passed explicitly (never closed over the ``for signal in
                signals`` loop variables — ruff B023 flags exactly that
                pattern as a latent bug class, even though this closure is
                always invoked within the same iteration) and their types
                stay narrowed (``composed`` is never ``None`` here).
                ``nonlocal session_id`` so the FIRST delivery of the run
                (clean OR caveat) lazily creates the shared standup thread
                exactly once."""
                nonlocal session_id
                text = _CAVEAT_PREFIX + composed.answer if caveat else composed.answer
                status = ledger.STATUS_DELIVERED_CAVEAT if caveat else ledger.STATUS_DELIVERED
                turn_id: int | None = None
                conclusion = distill_conclusion(composed.answer)  # unprefixed — see _CAVEAT_PREFIX
                if not dry_run:
                    if session_id is None:
                        session_id = _ensure_standup_session(db_path)
                    turn_id = _deliver(
                        conn, db_path, sig, text, composed.citations, session_id=session_id
                    )
                    ledger.record(
                        conn,
                        user_id=user_id,
                        signal=sig,
                        status=status,
                        now=now,
                        score=outcome.score,
                        session_id=session_id,
                        turn_id=turn_id,
                        conclusion=conclusion,
                    )
                if caveat:
                    report.delivered_caveat += 1
                else:
                    report.delivered += 1
                report.briefs.append(
                    DeliveredBrief(
                        kind=sig.kind,
                        ticker=sig.ticker,
                        headline=sig.headline,
                        score=outcome.score,
                        session_id=session_id or "(dry-run)",
                        turn_id=turn_id,
                        answer=text,
                    )
                )

            if outcome.passed:
                _record_and_deliver(signal, composed, outcome, caveat=False)
                continue

            if outcome.judge_failed:
                # An infra miss (CLI error / unparseable verdict), NOT a real
                # quality verdict — retry next run rather than lock the trip
                # out via STATUS_SUPPRESSED_EVAL's dedup window (see the
                # incident note on standup.ledger.STATUS_JUDGE_FAILED).
                report.judge_failed += 1
                log.warning(
                    {
                        "event": "standup_judge_failed",
                        "signal": signal.signature,
                        "rationale": outcome.rationale,
                    }
                )
                if not dry_run:
                    ledger.record(
                        conn,
                        user_id=user_id,
                        signal=signal,
                        status=ledger.STATUS_JUDGE_FAILED,
                        now=now,
                        score=outcome.score,
                    )
                continue

            # score=None only occurs under judge_failed (handled above) — the
            # narrow is defense-in-depth so a future infra shape can never be
            # compared against the caveat floor as if it were a measurement.
            if outcome.score is not None and outcome.score >= cfg.caveat_floor:
                # GENUINELY scored (the judge ran and returned a real
                # verdict) but below the standup's extra-caution floor —
                # deliver with a caveat rather than bury it (the channel was
                # otherwise silent 75%+ of the time; see cfg.caveat_floor).
                _record_and_deliver(signal, composed, outcome, caveat=True)
                continue

            report.suppressed_eval += 1
            log.info(
                {
                    "event": "standup_suppressed_eval",
                    "signal": signal.signature,
                    "score": round(outcome.score, 3) if outcome.score is not None else None,
                    "min_score": cfg.min_score,
                    "caveat_floor": cfg.caveat_floor,
                }
            )
            if not dry_run:
                ledger.record(
                    conn,
                    user_id=user_id,
                    signal=signal,
                    status=ledger.STATUS_SUPPRESSED_EVAL,
                    now=now,
                    score=outcome.score,
                )
        return report
    finally:
        conn.close()


__all__ = [
    "STANDUP_SESSION_SCOPE",
    "STANDUP_SESSION_TITLE",
    "DeliveredBrief",
    "StandupReport",
    "run_standup",
]
