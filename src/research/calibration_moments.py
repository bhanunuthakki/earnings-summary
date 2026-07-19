"""calibration_finding — the governor's zero-LLM calibration-confrontation class.

Program review 2026-07-19: the owner's own graded ledger contained a textbook
finding — high-conviction calls at 5 correct / 8 wrong (33% hit-rate vs the
~75% the label implies) — that no surface ever delivered. The July scorecard's
LLM leg failed transiently in a quota-dead window and persisted "no biases met
the grounding bar"; the deterministic substrate that could have said it needed
no LLM at all.

This collector reads ``decision_calibration.build_calibration`` directly and
emits AT MOST ONE moment per period when a conviction cohort crosses a
deterministic bar: hit-rate below ``_HIT_RATE_BAR`` with at least
``_MIN_GRADED`` graded calls (the Wilson floor discipline — a thin cohort
can't carry an accusation). The moment key is
``calibration_finding:<YYYY-MM>:<cohort>``, so the durable coach_pings ledger
(UNIQUE class+key) makes it fire once per period per cohort — a persistent
pattern re-surfaces monthly until it heals, never daily.

Receipts-first body (the owner's action-UX bar): counts, rate, and what the
label implies — never a vague "consider reviewing your process".
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from decision_calibration import build_calibration

# A high/medium-conviction cohort graded below this rate is miscalibrated
# enough to confront; 'low'/'unstated' cohorts are excluded (a low-conviction
# miss is not a calibration failure). Bars are deliberately coarse — this is a
# confrontation trigger, not a statistics lesson.
_HIT_RATE_BAR = 0.50
_MIN_GRADED = 10
_CONFRONTABLE = ("high", "medium")

# What the label is supposed to mean, for the receipt line.
_IMPLIED = {"high": 0.75, "medium": 0.60}


def collect_calibration_finding_moments(
    db_path: Path | str,
    *,
    now: datetime,
) -> list[object]:
    """≤1 Moment when a confrontable conviction cohort is graded below the bar.

    Returns governor ``Moment`` objects (imported lazily — the governor imports
    this module inside ``collect_moments``, mirroring ``capacity_moments``).
    Degrades to [] on any substrate problem; the governor's other classes must
    never be broken by this one."""
    from research.governor import Moment

    try:
        stats = build_calibration(db_path=db_path)
    except Exception:
        return []
    if stats is None:
        return []

    period = now.strftime("%Y-%m")
    worst: tuple[float, object] | None = None
    for bucket in stats.by_conviction:
        if bucket.conviction not in _CONFRONTABLE:
            continue
        if bucket.graded < _MIN_GRADED or bucket.hit_rate is None:
            continue
        if bucket.hit_rate >= _HIT_RATE_BAR:
            continue
        if worst is None or bucket.hit_rate < worst[0]:
            worst = (bucket.hit_rate, bucket)
    if worst is None:
        return []

    b = worst[1]
    implied = _IMPLIED.get(str(b.conviction), _HIT_RATE_BAR)
    band = (
        f" (95% CI {b.wilson_low:.0%}-{b.wilson_high:.0%})"
        if b.wilson_low is not None and b.wilson_high is not None
        else ""
    )
    body = (
        f"Calibration check: your {b.conviction}-conviction calls are graded "
        f"{b.correct} correct / {b.wrong} wrong / {b.mixed} mixed — a {b.hit_rate:.0%} "
        f"hit-rate{band} against the ~{implied:.0%} the label implies. "
        f"Worth sizing the next {b.conviction}-conviction add as if it were "
        f"one notch lower until the rate recovers."
    )
    return [
        Moment(
            class_="calibration_finding",
            key=f"calibration_finding:{period}:{b.conviction}",
            ticker=None,
            body=body,
            source_ref=f"calibration:{period}",
        )
    ]
