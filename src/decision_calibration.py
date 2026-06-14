"""Decisions-history calibration — deterministic SQL, no LLM (S15 PR2).

The decisions ledger (0046/0086) accumulates graded outcomes, but the panel
only ever showed the raw timeline. This module computes the analysis lenses
the directive names, all from plain aggregation:

  hit rate by conviction   — of the decisions stated at each conviction
      level, how many graded correct? The denominator is GRADED decisions
      (correct + wrong + mixed); pending and unfalsifiable rows are shown
      but never scored — a hit rate over unresolved calls would flatter.
  reversal patterns        — every ``user_action_kind='reversed'`` row,
      crossed with how the recommendation eventually graded: reversing a
      call that graded *wrong* vindicates the override; reversing one that
      graded *correct* cost money. The action mix (followed / ignored /
      partial / reversed / unacted) frames how often the owner overrides
      at all.
  time-to-outcome          — days from ``made_at`` to ``outcome_at`` per
      recommendation kind (mean + median): how long a call stays open
      before it can be judged.

``build_calibration`` returns None when the DB or decisions table is absent
(the panel hides the section) — the decision_extractor best-effort posture.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median

CONVICTION_ORDER: tuple[str, ...] = ("high", "medium", "low", "unstated")
GRADED_LABELS: frozenset[str] = frozenset({"correct", "wrong", "mixed"})
ACTION_KINDS: tuple[str, ...] = ("followed", "ignored", "partial", "reversed")


def bucket_for_conviction(value: object) -> str:
    """Map a stated conviction to a calibration cohort bucket — the one place
    the two conviction vocabularies are reconciled. ``decisions.conviction`` is
    already a high|medium|low string; ``position_sizing_intent`` states it
    numerically as n/5. Both land in CONVICTION_ORDER so the focus-cohort
    lookup (L1) and the advisor's per-name calibration (L1/L8) compare like
    with like. Anything unrecognised → 'unstated'."""
    if value is None:
        return "unstated"
    if isinstance(value, str):
        v = value.strip().lower()
        return v if v in CONVICTION_ORDER else "unstated"
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "unstated"
    if n >= 4.0:
        return "high"
    if n >= 3.0:
        return "medium"
    if n >= 1.0:
        return "low"
    return "unstated"


@dataclass(frozen=True, slots=True)
class ConvictionBucket:
    """Outcome distribution of one stated-conviction level."""

    conviction: str  # high | medium | low | unstated
    graded: int  # correct + wrong + mixed
    correct: int
    wrong: int
    mixed: int
    ungraded: int  # pending / unfalsifiable / not yet judged
    hit_rate: float | None  # correct / graded; None when nothing graded


@dataclass(frozen=True, slots=True)
class ReversalRecord:
    """One decision the owner reversed, with the eventual verdict."""

    decision_id: int
    ticker: str
    kind: str
    made_at: str  # ISO date
    outcome_label: str | None
    # True — the call graded wrong, the override was right. False — the call
    # graded correct, the override cost. None — still unresolved/mixed.
    vindicated: bool | None


@dataclass(frozen=True, slots=True)
class KindTiming:
    """Days from made_at to outcome_at for one recommendation kind."""

    kind: str
    n: int
    avg_days: float
    median_days: float


@dataclass(frozen=True, slots=True)
class CalibrationStats:
    """Everything the decisions panel's calibration section renders."""

    total: int
    graded: int
    overall_hit_rate: float | None
    by_conviction: list[ConvictionBucket]
    action_mix: dict[str, int]  # ACTION_KINDS + 'unacted'
    reversals: list[ReversalRecord]  # newest-first
    reversals_vindicated: int
    reversals_cost: int
    time_to_outcome: list[KindTiming]  # by kind, descending n


def _open(db_path: Path | str) -> sqlite3.Connection | None:
    try:
        path = Path(db_path)
        if not path.exists():
            return None
        conn = sqlite3.connect(str(path), timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='decisions'"
            ).fetchone()
            is None
        ):
            conn.close()
            return None
        return conn
    except (sqlite3.Error, OSError):
        return None


def _days_between(made_at: object, outcome_at: object) -> float | None:
    try:
        made = datetime.fromisoformat(str(made_at))
        graded = datetime.fromisoformat(str(outcome_at))
    except (TypeError, ValueError):
        return None
    # The ledger mixes naive and aware stamps across eras; compare naive.
    return (graded.replace(tzinfo=None) - made.replace(tzinfo=None)).total_seconds() / 86_400.0


def build_calibration(*, db_path: Path | str) -> CalibrationStats | None:
    """One pass over the decisions table → the three lenses. None when the
    substrate is absent; a present-but-empty ledger returns zeroed stats
    (the panel renders the how-to-populate hint instead of hiding)."""
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT id, ticker, recommendation_kind, conviction, made_at, "
            "user_action_kind, outcome_at, outcome_label FROM decisions"
        ).fetchall()
    finally:
        conn.close()

    counts: dict[str, dict[str, int]] = {
        c: {"correct": 0, "wrong": 0, "mixed": 0, "ungraded": 0} for c in CONVICTION_ORDER
    }
    action_mix: dict[str, int] = {k: 0 for k in (*ACTION_KINDS, "unacted")}
    reversals: list[ReversalRecord] = []
    durations: dict[str, list[float]] = {}
    graded_total = correct_total = 0

    for row in rows:
        label = str(row["outcome_label"] or "pending")
        is_graded = label in GRADED_LABELS

        conviction = str(row["conviction"] or "").lower()
        if conviction not in CONVICTION_ORDER:
            conviction = "unstated"
        bucket = counts[conviction]
        if is_graded:
            bucket[label] += 1
            graded_total += 1
            if label == "correct":
                correct_total += 1
        else:
            bucket["ungraded"] += 1

        action = str(row["user_action_kind"] or "").lower()
        action_mix[action if action in ACTION_KINDS else "unacted"] += 1
        if action == "reversed":
            vindicated: bool | None = None
            if label == "wrong":
                vindicated = True
            elif label == "correct":
                vindicated = False
            reversals.append(
                ReversalRecord(
                    decision_id=int(row["id"]),
                    ticker=str(row["ticker"]).upper(),
                    kind=str(row["recommendation_kind"]),
                    made_at=str(row["made_at"] or "")[:10],
                    outcome_label=None if label == "pending" else label,
                    vindicated=vindicated,
                )
            )

        if is_graded and row["outcome_at"] is not None:
            days = _days_between(row["made_at"], row["outcome_at"])
            if days is not None and days >= 0:
                durations.setdefault(str(row["recommendation_kind"]), []).append(days)

    by_conviction = [
        ConvictionBucket(
            conviction=c,
            graded=(g := b["correct"] + b["wrong"] + b["mixed"]),
            correct=b["correct"],
            wrong=b["wrong"],
            mixed=b["mixed"],
            ungraded=b["ungraded"],
            hit_rate=(b["correct"] / g) if g else None,
        )
        for c, b in counts.items()
        if (b["correct"] + b["wrong"] + b["mixed"] + b["ungraded"]) > 0
    ]
    reversals.sort(key=lambda r: r.made_at, reverse=True)
    timing = sorted(
        (
            KindTiming(
                kind=kind,
                n=len(days),
                avg_days=sum(days) / len(days),
                median_days=median(days),
            )
            for kind, days in durations.items()
        ),
        key=lambda t: -t.n,
    )
    return CalibrationStats(
        total=len(rows),
        graded=graded_total,
        overall_hit_rate=(correct_total / graded_total) if graded_total else None,
        by_conviction=by_conviction,
        action_mix=action_mix,
        reversals=reversals,
        reversals_vindicated=sum(1 for r in reversals if r.vindicated is True),
        reversals_cost=sum(1 for r in reversals if r.vindicated is False),
        time_to_outcome=timing,
    )
