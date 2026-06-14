"""Standup tuning knobs — detection thresholds, rate limits, the eval bar.

One frozen dataclass so the watchers (``standup.signals``) and the orchestrator
(``standup.run``) share a single source of truth, and the CLI rung
(``execution/run_standup.py``) can override any field from a flag. Defaults are
deliberately conservative: the standup pushes UNPROMPTED, so a chatty or
miscalibrated advisor is worse than silence — the thresholds bias toward not
speaking, and the eval ``min_score`` sits above the rubric's own pass bar.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StandupConfig:
    """Detection + rate-limit + eval-gate configuration for one standup run."""

    # --- detection thresholds (how trippy each watcher is) -----------------
    # A journal note open at least this many days is a stale open-item.
    journal_stale_days: int = 30
    # A DCF whose assumptions were valued at least this many days ago is stale.
    dcf_stale_days: int = 45
    # ...and only trips when |live-vs-fair| has also moved past this band, so a
    # stale-but-still-accurate model stays quiet (re-derive when it MATTERS).
    dcf_mispricing_pct: float = 20.0
    # A position whose live weight drifts at least this many percentage points
    # from its stated target weight trips the drift watcher.
    drift_min_pp: float = 3.0
    # Floor on a signal's deterministic materiality before it is even composed.
    min_materiality: float = 0.0
    # Hard cap on compose attempts per run (defence against a flood of trips).
    max_signals_considered: int = 25

    # --- rate limits (how often the advisor is allowed to speak) -----------
    # Most delivered messages per UTC day across the whole book.
    max_per_day: int = 3
    # A name that got a delivered standup stays quiet this many days after.
    per_name_cooldown_days: int = 3
    # The same trip (signature) does not re-compose — and re-pay the LLM —
    # within this window once it has been composed (delivered or suppressed).
    dedup_days: int = 7

    # --- the eval / relevance gate -----------------------------------------
    # The composed brief's eval overall must clear this to be delivered. >=
    # the ask_advisory_answer rubric's own pass threshold (0.70); set higher to
    # bias toward silence on borderline-quality briefs.
    min_score: float = 0.75
