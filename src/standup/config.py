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
    # An ungraded decision (outcome_at NULL) whose made_at is at least this many
    # days old has had its review horizon elapse — it's due for a verdict. Mirrors
    # decision_extractor.pending_for_grading's default older-than window.
    decision_verdict_days: int = 30
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
    # The deliver-with-caveat floor (navigation_ia.md §3.3 — "un-gag the
    # standup judge"). A GENUINELY-scored brief (the judge call succeeded and
    # returned a real facet_scores verdict — see standup.gate.GateOutcome.
    # judge_failed) in [caveat_floor, min_score) still reaches the thread,
    # prefixed with a one-line caveat, instead of being silently suppressed;
    # below caveat_floor it stays suppressed. A JUDGE-CALL FAILURE (infra, not
    # a quality verdict) is never eligible for the caveat tier regardless of
    # score — see standup.run.
    #
    # 0.60 chosen deliberately below the rubric's own pass_threshold (0.70,
    # evals/rubrics/ask_advisory_answer.md) by a margin roughly matching how
    # far above that threshold min_score already sits (0.75, +0.05): a brief
    # here failed the rubric's bar, but only modestly (facets averaging in
    # the high-0.6s) — worth showing with the caveat given the channel was
    # otherwise silent 75%+ of days (2026-07 audit). Below 0.60 the brief
    # meaningfully failed multiple facets (grounding / calibration / risk-
    # reward) and a bad, ungrounded advisory push is still worse than
    # silence — stays fully suppressed.
    #
    # No genuinely-low-but-real score currently exists to calibrate against:
    # the 2026-07 audit found EVERY historical suppression was actually a
    # judge-call failure (score=0.0 sentinel, not a real verdict — see
    # standup.ledger's STATUS_JUDGE_FAILED note), so this floor is a
    # considered a-priori choice, not a fit to observed data. Revisit once a
    # real distribution of genuinely-scored below-bar briefs accumulates.
    caveat_floor: float = 0.60
