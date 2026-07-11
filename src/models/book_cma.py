"""The wealthplan hand-off payload — ``execution/export_book_cma.py`` writes
one of these to ``data/dashboard/book_cma.json`` (directives/monthly_red_team.md
Phase 3: "the wealthplan CMA gets a quarterly export of realized book vol +
tail stats"). Validated with Pydantic before it ever touches disk (this repo's
schema-drift-defense rule): a shape drift here fails loud at export time, not
silently three hops downstream in another project's reader.

This repo NEVER writes into another project's directory — the contract is
strictly one-way: this file lands in earnings-summary's own ``data/dashboard/``
and the wealthplan project reads it from there on its own schedule.
"""

from __future__ import annotations

from pydantic import BaseModel


class TailStats(BaseModel):
    """One model's (normal or student_t) simulated annual book-return read.
    All ``*_pct`` fields are percent units; ``prob_below`` is a FRACTION
    (0-1) keyed by drawdown label ("-20%", "-30%", "-40%", "-50%")."""

    method: str
    mean_pct: float
    vol_pct: float
    pct_5th: float
    pct_1st: float
    prob_below: dict[str, float]


class BookVol(BaseModel):
    """Realized book volatility from two independent derivations of the same
    annual covariance — a closed-form cross-check on the simulation."""

    cov_based_pct: float  # sqrt(w' Sigma_annual w), no simulation noise
    simulated_normal_pct: float  # std dev of the simulated normal-model paths


class EventStressLeg(BaseModel):
    ticker: str
    weight_pct: float
    return_pct: float
    label: str


class EventStressExport(BaseModel):
    scenario_id: str
    title: str
    book_return_pct: float
    legs: list[EventStressLeg]
    notes: list[str] = []


class BookCmaExport(BaseModel):
    """The full quarterly hand-off: coverage, both vol derivations, both
    tail-distribution reads, and the joint-LatAm event stress. ``computed_at``
    / ``weights_as_of`` are naive-UTC ISO timestamps (project convention)."""

    computed_at: str
    weights_as_of: str | None
    tickers_modeled: list[str]
    n_tickers_modeled: int
    modeled_weight_pct: float
    unmodeled_weight_pct: float
    dropped: dict[str, str]
    wealthplan_cma_assumed_vol_pct: float
    book_vol: BookVol
    normal_tail: TailStats
    student_t_tail: TailStats
    t_df: int
    n_paths: int
    seed: int
    drift_source: str
    joint_latam_stress: EventStressExport | None = None
