"""src/credibility/ — the self-calibrating credibility engine (Close-the-Loops L10).

The per-fact ``confidence`` number (``pipeline.confidence``) is a hand-set
arithmetic prior that was never checked against reality. This package closes
that loop:

    observations  — the ground-truth ledger: every superseded fact and resolved
                    cross-source disagreement, graded against the confidence it
                    carried (``confidence_observations``, alembic 0106).
    calibration   — turns the ledger into a reliability table + Brier score per
                    confidence bucket / source tier (the measurement).
    priors        — a measured per-(tier × ticker × line-item-class) prior that
                    replaces the hand-set constant ONLY once enough observations
                    accrue (gated on ``calibration_guard``), falling back to the
                    constant until then.

The measurement (observations + calibration) ships first and is always-on; the
priors are wired into the confidence backfill behind the minimum-n gate so a
sparse denominator never overwrites a considered constant.
"""

from __future__ import annotations
