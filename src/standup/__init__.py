"""Proactive analyst standup (close_the_loops_2026_06.md L9).

Turns the advisor from reactive to initiating: a scheduled rung watches four
open loops — falsifiable decision conditions, stale journal open-items, DCF
assumption staleness, live position drift — and, when one trips, composes a
grounded, cited brief through the Ask engine, eval-gates it for relevance, and
writes it into a persistent ``ask_sessions`` thread the owner sees as a waiting
advisory message. Eval-gated and rate-limited by design: a chatty, miscalibrated
advisor that pushes unprompted is worse than silence.
"""

from __future__ import annotations

from standup.config import StandupConfig
from standup.run import (
    STANDUP_SESSION_SCOPE,
    STANDUP_SESSION_TITLE,
    DeliveredBrief,
    StandupReport,
    run_standup,
)
from standup.signals import StandupSignal, collect_signals

__all__ = [
    "STANDUP_SESSION_SCOPE",
    "STANDUP_SESSION_TITLE",
    "DeliveredBrief",
    "StandupConfig",
    "StandupReport",
    "StandupSignal",
    "collect_signals",
    "run_standup",
]
