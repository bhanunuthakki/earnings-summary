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
          below the bar?             -> record suppressed_eval, continue
        deliver: append [user headline] + [assistant brief] to the rolling
                 standup thread, distill the conclusion (memory stub),
                 record delivered.

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
from standup.compose import EventsFn, compose
from standup.config import StandupConfig
from standup.gate import gate_message
from standup.memory import distill_conclusion, recall_conclusion
from standup.signals import StandupSignal, collect_signals

log = logging.getLogger(__name__)

STANDUP_SESSION_TITLE = "Analyst standup"
STANDUP_SESSION_SCOPE = "portfolio"
_STANDUP_MODEL_TAG = "standup/ask_answer"


@dataclass(frozen=True, slots=True)
class DeliveredBrief:
    """One brief that cleared the gate and landed in the thread."""

    kind: str
    ticker: str | None
    headline: str
    score: float
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
    suppressed_eval: int = 0
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
            "suppressed_eval": self.suppressed_eval,
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
    conn = sqlite3.connect(str(db_path), timeout=5.0)
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
            if delivered_today + report.delivered >= cfg.max_per_day:
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
            if not outcome.passed:
                report.suppressed_eval += 1
                log.info(
                    {
                        "event": "standup_suppressed_eval",
                        "signal": signal.signature,
                        "score": round(outcome.score, 3),
                        "min_score": cfg.min_score,
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
                continue

            turn_id: int | None = None
            conclusion = distill_conclusion(composed.answer)
            if not dry_run:
                if session_id is None:
                    session_id = _ensure_standup_session(db_path)
                turn_id = _deliver(
                    conn,
                    db_path,
                    signal,
                    composed.answer,
                    composed.citations,
                    session_id=session_id,
                )
                ledger.record(
                    conn,
                    user_id=user_id,
                    signal=signal,
                    status=ledger.STATUS_DELIVERED,
                    now=now,
                    score=outcome.score,
                    session_id=session_id,
                    turn_id=turn_id,
                    conclusion=conclusion,
                )
            report.delivered += 1
            report.briefs.append(
                DeliveredBrief(
                    kind=signal.kind,
                    ticker=signal.ticker,
                    headline=signal.headline,
                    score=outcome.score,
                    session_id=session_id or "(dry-run)",
                    turn_id=turn_id,
                    answer=composed.answer,
                )
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
